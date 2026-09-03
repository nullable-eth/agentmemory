"""Filing agent (P4): turn unfiled .staging material into filing proposals.

Every FILING_INTERVAL_S the loop walks <VAULT_ROOT>/.staging/ and asks the
chat endpoint (CHAT_URL) where each unfiled file belongs — target node,
tags, confidence, rationale — recording each answer as a 'pending' row in
filing_proposals. With FILING_MODE=auto, proposals at/above
FILING_CONFIDENCE_MIN apply themselves; in propose mode (the default)
nothing applies except through the admin approval endpoints.

The apply is the transform proven in the vault's history (CLAUDE.md, step
4): stamp the frontmatter (status: filed, topical tags, project: wikilink)
and move the file into the node. The body after the closing --- is never
touched, an existing destination is never overwritten, and nothing is ever
deleted.
"""
import asyncio
import json
import logging
import re
import shutil
import sys
from pathlib import Path

import httpx

import db
import metrics
import scanner
import vaultio
from config import (FILING_BATCH, FILING_CONFIDENCE_MIN, FILING_INTERVAL_S,
                    CHAT_API_KEY, CHAT_MODEL, CHAT_URL,
                    FILING_LOW_CONF_NODE, FILING_MODE, VAULT_ROOT)

log = logging.getLogger("agentmemory")

REINDEX = Path(__file__).parent / "vaultops" / "reindex.py"

# One apply at a time, ever. Held across every apply — loop auto-apply and
# the admin approval endpoints alike — and the reindex+rescan that follows.
apply_lock = asyncio.Lock()


# ------------------------------------------------------------- node targets
def _nodes() -> list[str]:
    """Live node list, rebuilt fresh each cycle: directories directly under
    VAULT_ROOT whose names don't start with '.'. The only valid targets."""
    root = Path(VAULT_ROOT)
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


def _dest_and_link(rel: str, node: str) -> tuple[str, str]:
    """Destination path + project wikilink, by source location. The em-dash
    is U+2014, exactly as existing files use it."""
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith(".staging/Chats/Full Transcripts/"):
        return (f"{node}/Chats/Full Transcripts/{name}",
                f"[[../../{node} \u2014 Index|{node}]]")
    if rel.startswith(".staging/Project Files/"):
        return (f"{node}/Project Files/{name}",
                f"[[../{node} \u2014 Index|{node}]]")
    return f"{node}/{name}", f"[[{node} \u2014 Index|{node}]]"


# --------------------------------------------------------------------- apply
def _stamp(text: str, link: str, tags: list[str]) -> str:
    """Frontmatter-only mutation of an unfiled file:
      - status: unfiled -> status: filed
      - the literal empty 'tags: []' line -> the proposed tags (comma-space
        separated, no quotes); any other tags line is left untouched
      - a project: line appended as the last frontmatter line if absent
    The body after the closing --- is returned byte-identical."""
    m = vaultio.FM_RE.match(text)
    if not m:
        raise RuntimeError("no frontmatter to stamp")
    fm, body = m.group(1), text[m.end():]
    fm, n = re.subn(r"(?m)^status:[ \t]*unfiled[ \t]*$", "status: filed",
                    fm, count=1)
    if n != 1:
        raise RuntimeError("status: unfiled line not found")
    if tags:
        fm, _ = re.subn(r"(?m)^tags:[ \t]*\[[ \t]*\][ \t]*$",
                        f"tags: [{', '.join(tags)}]", fm, count=1)
    if not re.search(r"(?m)^project:", fm):
        fm += f'project: "{link}"' + "\n"
    return "---\n" + fm + "---\n" + body


async def apply_proposal(proposal_row) -> str:
    """Apply one pending proposal: stamp the frontmatter, move the file into
    the proposed node. Returns the new vault-relative path; raises
    RuntimeError on failure. Marks the row rejected (with a note appended to
    the rationale) when the file has gone stale or the destination already
    exists. The caller must hold apply_lock."""
    pid = proposal_row["id"]
    rel = proposal_row["path"]
    node = proposal_row["proposed_node"]
    tags = list(proposal_row["proposed_tags"] or [])

    pool = await db.pool()
    async with pool.acquire() as c:
        fresh = await db.get_proposal(c, pid)
    if not fresh or fresh["status"] != "pending":
        raise RuntimeError(f"proposal {pid} is no longer pending")
    if node not in _nodes():
        async with pool.acquire() as c:
            await db.reject_proposal(c, pid, " [stale]")
        raise RuntimeError(f"proposal {pid} stale: node {node!r} no longer exists")

    src = Path(VAULT_ROOT) / rel
    try:
        text = src.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    if text is None or vaultio.parse_frontmatter(text).get("status") != "unfiled":
        async with pool.acquire() as c:
            await db.reject_proposal(c, pid, " [stale]")
        raise RuntimeError(f"proposal {pid} stale: {rel} missing or no longer unfiled")

    dest_rel, link = _dest_and_link(rel, node)
    stamped = _stamp(text, link, tags)

    dest = Path(VAULT_ROOT) / dest_rel
    if dest.exists():
        async with pool.acquire() as c:
            await db.reject_proposal(c, pid, " [destination exists]")
        raise RuntimeError(f"proposal {pid}: destination exists ({dest_rel})")

    src.write_bytes(stamped.encode("utf-8"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))

    async with pool.acquire() as c:
        await db.mark_proposal_applied(c, pid)
    return dest_rel


async def reindex_and_rescan() -> None:
    """Rebuild the generated graph material, then rescan the vault. A
    nonzero reindex exit is logged at ERROR but never undoes the move."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(REINDEX), "--vault", VAULT_ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if proc.returncode == 0:
        log.info("filing: reindex succeeded:\n%s", text)
    else:
        log.error("filing: reindex failed (exit %d) — move stands:\n%s",
                  proc.returncode, text)
    await scanner.scan_once()


async def approve_one(proposal_id: int) -> str:
    """Admin single-approve: apply one pending proposal, then reindex+rescan
    once. Raises LookupError (unknown id), ValueError (not pending), or
    RuntimeError (apply failed)."""
    async with apply_lock:
        pool = await db.pool()
        async with pool.acquire() as c:
            row = await db.get_proposal(c, proposal_id)
        if not row:
            raise LookupError(f"no such proposal {proposal_id}")
        if row["status"] != "pending":
            raise ValueError(f"proposal {proposal_id} is {row['status']}, not pending")
        new_path = await apply_proposal(row)
        await reindex_and_rescan()
    return new_path


async def approve_many() -> tuple[int, int]:
    """Admin approve-all: every pending proposal at/above the confidence
    floor. Individual failures are logged and counted, never fatal; the
    reindex+rescan runs once at the end. Returns (applied, failed)."""
    pool = await db.pool()
    async with pool.acquire() as c:
        rows = await db.pending_above_floor(c, FILING_CONFIDENCE_MIN)
    applied = failed = 0
    async with apply_lock:
        for row in rows:
            try:
                new_path = await apply_proposal(row)
            except Exception:
                failed += 1
                log.exception("filing: approve_all: apply failed for %s",
                              row["path"])
            else:
                applied += 1
                log.info("filing: approve_all: %s -> %s", row["path"], new_path)
        if applied:
            await reindex_and_rescan()
    return applied, failed


# -------------------------------------------------------------- propose loop
def _system_prompt(nodes: list[str]) -> str:
    """CLAUDE.md + every node's Scope.md + the literal node list + the
    answer-format contract."""
    root = Path(VAULT_ROOT)
    parts = []
    claude = root / "CLAUDE.md"
    if claude.is_file():
        parts.append(claude.read_text(encoding="utf-8-sig", errors="replace"))
    for n in nodes:
        scope = root / n / "Scope.md"
        if scope.is_file():
            parts.append(scope.read_text(encoding="utf-8-sig", errors="replace"))
    parts.append("The valid nodes — the only places a file may be filed into — "
                 "are: " + ", ".join(nodes))
    parts.append(
        "Answer with ONLY a JSON object of exactly this shape and nothing "
        "else: {\"node\": <one of the listed nodes>, "
        "\"tags\": [3-5 short lowercase strings], "
        "\"confidence\": <0..1 float>, "
        "\"rationale\": <string under 200 chars>}")
    return "\n\n".join(parts)


def _user_prompt(rel: str, fm: dict, body: str) -> str:
    return (f"Title: {fm.get('title') or Path(rel).stem}\n"
            f"Date: {fm.get('date') or ''}\n"
            f"Tags: {', '.join(fm.get('_tags') or [])}\n\n"
            f"{body[:4000]}")


async def _chat(client: httpx.AsyncClient, system: str, user: str) -> str:
    headers = ({"Authorization": f"Bearer {CHAT_API_KEY}"}
               if CHAT_API_KEY else {})
    r = await client.post(
        f"{CHAT_URL.rstrip('/')}/v1/chat/completions",
        headers=headers,
        json={"model": CHAT_MODEL, "temperature": 0.1,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=300)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse_reply(raw: str, nodes: list[str]) -> dict | None:
    """Parse + validate one chat reply. None = unusable: the caller logs at
    WARNING, inserts nothing, and moves on (never retried in this cycle)."""
    t = (raw or "").strip()
    fence = re.match(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        obj = json.loads(t)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    node, tags, conf = obj.get("node"), obj.get("tags"), obj.get("confidence")
    if not isinstance(node, str) or node not in nodes:
        return None
    if not isinstance(tags, list) or not all(isinstance(x, str) for x in tags):
        return None
    if (isinstance(conf, bool) or not isinstance(conf, (int, float))
            or not 0.0 <= float(conf) <= 1.0):
        return None
    rationale = obj.get("rationale")
    return {"node": node, "tags": tags, "confidence": float(conf),
            "rationale": (str(rationale) if rationale is not None else "")[:200]}


async def _candidates() -> list[tuple[str, str]]:
    """Unfiled .staging markdown with no live (pending/applied) proposal,
    capped at FILING_BATCH. Returns (vault-relative posix path, full text)."""
    root = Path(VAULT_ROOT)
    staging = root / ".staging"
    if not staging.is_dir():
        return []
    pool = await db.pool()
    async with pool.acquire() as c:
        busy = await db.busy_paths(c)
    out: list[tuple[str, str]] = []
    for f in sorted(staging.rglob("*.md")):
        if f.name == "README.md":
            continue
        rel_parts = f.relative_to(staging).parts
        if rel_parts and rel_parts[0] == "superseded":
            continue
        rel = f.relative_to(root).as_posix()
        if rel in busy:
            continue
        try:
            text = f.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if vaultio.parse_frontmatter(text).get("status") != "unfiled":
            continue
        out.append((rel, text))
        if len(out) >= FILING_BATCH:
            break
    return out


async def run_cycle() -> None:
    """One filing pass. filing_loop guards the whole body, so nothing here
    may kill the loop."""
    if not CHAT_URL:
        return
    nodes = _nodes()
    if not nodes:
        log.warning("filing: no nodes under %s; skipping cycle", VAULT_ROOT)
        return
    candidates = await _candidates()
    pool = await db.pool()
    auto_applied = 0
    if candidates:
        system = _system_prompt(nodes)
        async with httpx.AsyncClient() as client:
            for rel, text in candidates:
                fm = vaultio.parse_frontmatter(text)
                m = vaultio.FM_RE.match(text)
                body = text[m.end():] if m else text
                try:
                    raw = await _chat(client, system, _user_prompt(rel, fm, body))
                except (httpx.HTTPError, KeyError, ValueError, IndexError) as e:
                    log.warning("filing: chat call failed for %s: %s", rel, e)
                    continue
                parsed = _parse_reply(raw, nodes)
                if parsed is None:
                    log.warning("filing: unusable reply for %s: %.300s", rel, raw)
                    continue
                # Auto-mode catch-all: below-floor items reroute to the
                # configured catch-all node (original proposal preserved in
                # the rationale, so a later audit can promote them out).
                below = parsed["confidence"] < FILING_CONFIDENCE_MIN
                rerouted = False
                if (FILING_MODE == "auto" and below
                        and FILING_LOW_CONF_NODE
                        and FILING_LOW_CONF_NODE in nodes):
                    parsed = {
                        "node": FILING_LOW_CONF_NODE,
                        "tags": parsed["tags"],
                        "confidence": parsed["confidence"],
                        "rationale": (f"[catch-all; model proposed "
                                      f"{parsed['node']} @ "
                                      f"{parsed['confidence']:.2f}] "
                                      + parsed["rationale"])[:200],
                    }
                    rerouted = True
                async with pool.acquire() as c:
                    pid = await db.insert_proposal(
                        c, path=rel, node=parsed["node"], tags=parsed["tags"],
                        confidence=parsed["confidence"],
                        rationale=parsed["rationale"])
                log.info("filing: proposal %d: %s -> %s (%.2f)",
                         pid, rel, parsed["node"], parsed["confidence"])
                if FILING_MODE == "auto":
                    if (not below) or rerouted:
                        row = {"id": pid, "path": rel,
                               "proposed_node": parsed["node"],
                               "proposed_tags": parsed["tags"],
                               "confidence": parsed["confidence"],
                               "rationale": parsed["rationale"]}
                        try:
                            async with apply_lock:
                                new_path = await apply_proposal(row)
                        except Exception:
                            log.exception("filing: auto-apply failed for %s", rel)
                        else:
                            auto_applied += 1
                            metrics.AUTO_APPLIED.inc()
                            if rerouted:
                                metrics.BELOW_FLOOR.inc()
                            log.info("filing: auto-applied %s -> %s",
                                     rel, new_path)
                    else:
                        metrics.BELOW_FLOOR.inc()
    async with pool.acquire() as c:
        counts = await db.proposal_status_counts(c)
    for s in ("pending", "approved", "rejected", "applied"):
        metrics.PROPOSALS.labels(status=s).set(counts.get(s, 0))
    if auto_applied:
        async with apply_lock:
            await reindex_and_rescan()


async def filing_loop() -> None:
    while True:
        try:
            await run_cycle()
        except Exception:
            log.exception("filing loop error")
        await asyncio.sleep(FILING_INTERVAL_S)
