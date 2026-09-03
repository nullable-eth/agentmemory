"""AgentMemory DB layer — asyncpg, derived data only, fully rebuildable."""
import json
from pathlib import Path

import asyncpg

from config import PG_DSN, EMBED_DIM

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=8)
        async with _pool.acquire() as c:
            await c.execute((Path(__file__).parent / "schema.sql").read_text())
    return _pool


def _vec(v) -> str | None:
    return None if v is None else "[" + ",".join(f"{x:.6g}" for x in v) + "]"


async def upsert_document(c, *, path, source_uuid, node, title, date, doc_type,
                          tags, chash, mtime) -> int:
    return await c.fetchval(
        """INSERT INTO documents (path, source_uuid, node, title, date,
                                  doc_type, tags, content_hash, mtime)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
           ON CONFLICT (path) DO UPDATE SET
             source_uuid=$2, node=$3, title=$4, date=$5, doc_type=$6,
             tags=$7, content_hash=$8, mtime=$9, indexed_at=now()
           RETURNING id""",
        path, source_uuid, node, title, date, doc_type, tags, chash, mtime)


async def replace_chunks(c, document_id: int, rows):
    """rows: (message_uuid, ordinal, sender, created_at, text).
    Delete-and-insert per document; embeddings for unchanged text are
    preserved by carrying them over on identical (uuid, ordinal, text)."""
    old = {(r["message_uuid"], r["ordinal"]): (r["text"], r["embedding"])
           for r in await c.fetch(
               "SELECT message_uuid, ordinal, text, embedding FROM chunks "
               "WHERE document_id=$1", document_id)}
    await c.execute("DELETE FROM chunks WHERE document_id=$1", document_id)
    ins = []
    for mu, o, sender, created, text in rows:
        prev = old.get((mu, o))
        emb = prev[1] if prev and prev[0] == text else None
        ins.append((document_id, mu, o, sender, created, text, emb))
    await c.executemany(
        """INSERT INTO chunks (document_id, message_uuid, ordinal, sender,
                               created_at, text, embedding)
           VALUES ($1,$2,$3,$4,
                   NULLIF($5,'')::timestamptz, $6, $7::vector)""",
        [(d, mu, o, s, cr or "", t, e) for d, mu, o, s, cr, t, e in ins])


async def fetch_embed_backlog(c, limit: int):
    return await c.fetch(
        "SELECT id, text FROM chunks WHERE embedding IS NULL AND NOT embed_failed "
        "ORDER BY id LIMIT $1", limit)


async def mark_embed_failed(c, chunk_id: int):
    await c.execute("UPDATE chunks SET embed_failed=true WHERE id=$1", chunk_id)


async def store_embeddings(c, pairs):
    await c.executemany(
        "UPDATE chunks SET embedding=$2::vector WHERE id=$1",
        [(cid, _vec(v)) for cid, v in pairs])


async def hybrid_search(c, *, qvec, qtext, node=None, tags=None,
                        after=None, before=None, k=10):
    """RRF fusion of dense (cosine) + lexical (websearch tsquery) rank lists.
    Either leg may be absent (no embedding yet / stopword-only query)."""
    return await c.fetch(
        """WITH filt AS (
             SELECT ch.id, ch.document_id, ch.message_uuid, ch.ordinal,
                    ch.sender, ch.created_at, ch.text, ch.embedding, ch.tsv,
                    d.path, d.node, d.title, d.date, d.tags
             FROM chunks ch JOIN documents d ON d.id = ch.document_id
             WHERE ($3::text  IS NULL OR d.node = $3)
               AND ($4::text[] IS NULL OR d.tags && $4)
               AND ($5::date  IS NULL OR d.date >= $5)
               AND ($6::date  IS NULL OR d.date <= $6)
           ),
           dense AS (
             SELECT id, row_number() OVER (ORDER BY embedding <=> $1::vector) rnk
             FROM filt WHERE $1::text IS NOT NULL AND embedding IS NOT NULL
             ORDER BY embedding <=> $1::vector LIMIT 60
           ),
           lex AS (
             SELECT id, row_number() OVER
                    (ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('english',$2)) DESC) rnk
             FROM filt
             WHERE $2 <> '' AND tsv @@ websearch_to_tsquery('english', $2)
             LIMIT 60
           ),
           fused AS (
             SELECT COALESCE(dense.id, lex.id) AS id,
                    COALESCE(1.0/(60+dense.rnk),0) + COALESCE(1.0/(60+lex.rnk),0) AS score
             FROM dense FULL OUTER JOIN lex USING (id)
           )
           SELECT f.score, ft.path, ft.node, ft.title, ft.date, ft.tags,
                  ft.message_uuid, ft.ordinal, ft.sender, ft.created_at,
                  left(ft.text, 500) AS snippet
           FROM fused f JOIN filt ft ON ft.id = f.id
           ORDER BY f.score DESC LIMIT $7""",
        _vec(qvec), qtext or "", node, tags, after, before, k)


# ---------------------------------------------------- filing proposals (P4)
async def insert_proposal(c, *, path, node, tags, confidence, rationale) -> int:
    return await c.fetchval(
        """INSERT INTO filing_proposals (path, proposed_node, proposed_tags,
                                         confidence, rationale, status)
           VALUES ($1,$2,$3,$4,$5,'pending') RETURNING id""",
        path, node, tags, confidence, rationale)


async def get_proposal(c, proposal_id: int):
    return await c.fetchrow(
        """SELECT id, path, proposed_node, proposed_tags, confidence, rationale,
                  status, created_at, decided_at
           FROM filing_proposals WHERE id=$1""", proposal_id)


async def list_proposals(c, status: str = "pending"):
    return await c.fetch(
        """SELECT id, path, proposed_node, proposed_tags, confidence, rationale,
                  status, created_at
           FROM filing_proposals WHERE status=$1 ORDER BY id""", status)


async def busy_paths(c) -> set:
    """Paths with a live (pending/applied) proposal — not re-proposable."""
    return {r["path"] for r in await c.fetch(
        "SELECT path FROM filing_proposals WHERE status IN ('pending','applied')")}


async def pending_above_floor(c, floor: float):
    return await c.fetch(
        """SELECT id, path, proposed_node, proposed_tags, confidence, rationale,
                  status, created_at
           FROM filing_proposals
           WHERE status='pending' AND confidence >= $1 ORDER BY id""", floor)


async def reject_proposal(c, proposal_id: int, note: str):
    """Mark rejected, appending note to the existing rationale."""
    await c.execute(
        """UPDATE filing_proposals
           SET status='rejected', rationale=COALESCE(rationale,'') || $2,
               decided_at=now()
           WHERE id=$1""", proposal_id, note)


async def set_proposal_status(c, proposal_id: int, status: str):
    await c.execute(
        "UPDATE filing_proposals SET status=$2, decided_at=now() WHERE id=$1",
        proposal_id, status)


async def mark_proposal_applied(c, proposal_id: int):
    await c.execute(
        "UPDATE filing_proposals SET status='applied', decided_at=now() "
        "WHERE id=$1", proposal_id)


async def proposal_status_counts(c) -> dict:
    rows = await c.fetch(
        "SELECT status, count(*) AS n FROM filing_proposals GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


async def staging_counts(c) -> dict:
    rows = await c.fetch(
        """SELECT CASE
             WHEN p.status = 'pending' THEN 'pending_approval'
             WHEN p.status IS NULL THEN 'unproposed'
             ELSE 'below_floor' END AS state, count(*) AS n
           FROM documents d
           LEFT JOIN filing_proposals p
             ON p.path = d.path AND p.status IN ('pending','rejected')
           WHERE d.path LIKE '.staging/%' GROUP BY 1""")
    out = {"unproposed": 0, "pending_approval": 0, "below_floor": 0}
    out.update({r["state"]: r["n"] for r in rows})
    return out
