"""Polling scanner (CIFS has no inotify) + BGE-M3 embed backlog drain."""
import asyncio
import logging
import time
from datetime import date
from pathlib import Path

import httpx

import db
import metrics
import vaultio
from config import (EMBED_BATCH, EMBED_MAX_CHARS, EMBED_URL, MODEL_API_KEY,
                    SCAN_INTERVAL_S, SKIP_DIRS, VAULT_ROOT)

log = logging.getLogger("agentmemory")


def _rel(p: Path) -> str:
    return p.relative_to(VAULT_ROOT).as_posix()


def _doc_type(rel: str, fm: dict) -> str:
    if "/Chats/Full Transcripts/" in f"/{rel}":
        return "transcript"
    if rel.startswith(".staging/"):
        return "staging"
    t = fm.get("type", "")
    return t or ("scope" if rel.endswith("Scope.md") else "note")


def _date(fm: dict, rel: str):
    for src in (fm.get("date", ""), Path(rel).name[:10]):
        try:
            return date.fromisoformat(src[:10])
        except (ValueError, TypeError):
            continue
    return None


async def scan_once() -> int:
    """Sweep the vault; (re)index changed markdown. Returns files touched."""
    root = Path(VAULT_ROOT)
    touched = 0
    p = await db.pool()
    async with p.acquire() as c:
        seen = set()
        for f in root.rglob("*.md"):
            parts = f.relative_to(root).parts
            if parts and parts[0] in SKIP_DIRS:
                continue
            rel = _rel(f)
            seen.add(rel)
            st = f.stat()
            row = await c.fetchrow(
                "SELECT mtime, size, content_hash FROM scan_state WHERE path=$1", rel)
            if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
                continue
            text = f.read_text(encoding="utf-8-sig", errors="replace")
            chash = vaultio.content_hash(text)
            if row and row["content_hash"] == chash:
                await c.execute(
                    "UPDATE scan_state SET mtime=$2, size=$3, last_seen=now() "
                    "WHERE path=$1", rel, st.st_mtime, st.st_size)
                continue
            await index_file(c, rel, text, st.st_mtime)
            await c.execute(
                """INSERT INTO scan_state (path, mtime, size, content_hash)
                   VALUES ($1,$2,$3,$4) ON CONFLICT (path) DO UPDATE SET
                   mtime=$2, size=$3, content_hash=$4, last_seen=now()""",
                rel, st.st_mtime, st.st_size, chash)
            touched += 1
        # files deleted or moved out from under us
        gone = [r["path"] for r in await c.fetch("SELECT path FROM scan_state")
                if r["path"] not in seen]
        for rel in gone:
            await c.execute("DELETE FROM documents WHERE path=$1", rel)
            await c.execute("DELETE FROM scan_state WHERE path=$1", rel)
            touched += 1
    metrics.LAST_SCAN.set(time.time())
    return touched


async def index_file(c, rel: str, text: str, mtime: float):
    fm = vaultio.parse_frontmatter(text)
    m = vaultio.FM_RE.match(text)
    body = text[m.end():] if m else text
    dtype = _doc_type(rel, fm)
    node = rel.split("/", 1)[0] if "/" in rel and not rel.startswith(".") else None
    doc_id = await db.upsert_document(
        c, path=rel, source_uuid=fm.get("source_uuid"), node=node,
        title=fm.get("title") or Path(rel).stem, date=_date(fm, rel),
        doc_type=dtype, tags=fm.get("_tags", []),
        chash=vaultio.content_hash(text), mtime=mtime)
    if dtype in ("transcript", "staging") and vaultio.MSG_RE.search(body):
        rows = list(vaultio.chunk_transcript(body, EMBED_MAX_CHARS))
    else:
        rows = list(vaultio.chunk_note(body, EMBED_MAX_CHARS))
    await db.replace_chunks(c, doc_id, rows)


async def embed_batch(client: httpx.AsyncClient, texts):
    r = await client.post(
        f"{EMBED_URL.rstrip('/')}/v1/embeddings",
        headers={"Authorization": f"Bearer {MODEL_API_KEY}"} if MODEL_API_KEY else {},
        json={"input": texts, "model": "bge-m3"}, timeout=120)
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


async def _embed_one_resilient(client, c, chunk_id: int, text: str) -> bool:
    """Try full, then progressively halved prefixes of the EMBED COPY (stored
    text stays verbatim; a prefix embedding beats no dense leg at all).
    Persistent failure marks the chunk embed_failed — lexical-only forever
    rather than blocking the backlog."""
    t = vaultio.embed_text(text) or " "
    while len(t) >= 400:
        try:
            vecs = await embed_batch(client, [t])
            await db.store_embeddings(c, [(chunk_id, vecs[0])])
            return True
        except httpx.HTTPStatusError:
            t = t[: len(t) // 2]
        except httpx.HTTPError:
            raise                      # transport problem, not this chunk's fault
    await db.mark_embed_failed(c, chunk_id)
    metrics.EMBED_FAILED.inc()
    log.warning("chunk %d unembeddable even truncated; marked embed_failed", chunk_id)
    return False


async def embed_drain():
    """Drain the null-embedding backlog; resumable, state lives in the DB.
    A failing batch degrades to per-item; a failing item degrades to truncated
    prefixes; a hopeless item is marked and skipped — never head-of-line."""
    if not EMBED_URL:
        return 0
    done = 0
    p = await db.pool()
    async with httpx.AsyncClient() as client:
        while True:
            async with p.acquire() as c:
                rows = await db.fetch_embed_backlog(c, EMBED_BATCH)
                if not rows:
                    break
                try:
                    vecs = await embed_batch(
                        client, [vaultio.embed_text(r["text"]) or " " for r in rows])
                    await db.store_embeddings(c, list(zip([r["id"] for r in rows], vecs)))
                    done += len(rows)
                except httpx.HTTPStatusError:
                    for r in rows:      # isolate the poison item(s)
                        if await _embed_one_resilient(client, c, r["id"], r["text"]):
                            done += 1
                metrics.DEP_UP.labels(dep="embeddings").set(1)
    return done


async def scan_loop():
    while True:
        try:
            n = await scan_once()
            if n:
                log.info("scan: %d file(s) reindexed", n)
            e = await embed_drain()
            if e:
                log.info("embedded %d chunk(s)", e)
        except Exception:
            log.exception("scan/embed loop error")
            metrics.DEP_UP.labels(dep="embeddings").set(0)
        p = await db.pool()
        async with p.acquire() as c:
            backlog = await c.fetchval(
                "SELECT count(*) FROM chunks WHERE embedding IS NULL AND NOT embed_failed")
            metrics.EMBED_BACKLOG.set(backlog)
            for k, v in (await db.staging_counts(c)).items():
                metrics.STAGING.labels(state=k).set(v)
        await asyncio.sleep(SCAN_INTERVAL_S)
