"""AgentMemory API — search over the vault; loops run in-process.

Auth: READ_TOKEN for search/context, ADMIN_TOKEN for scan. Readiness gates on
PG + vault mount only (spec): search must survive model outages.
"""
import asyncio
import logging
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest

import db
import metrics
import scanner
import vaultio
from config import (ADMIN_TOKEN, EMBED_URL, MODEL_API_KEY, READ_TOKEN,
                    VAULT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = FastAPI(title="agentmemory")


def _auth(token: str):
    async def dep(authorization: str = Header(default="")):
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401)
    return dep


read_auth, admin_auth = _auth(READ_TOKEN), _auth(ADMIN_TOKEN)


@app.on_event("startup")
async def startup():
    await db.pool()
    asyncio.create_task(scanner.scan_loop())


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    if not Path(VAULT_ROOT, "CLAUDE.md").exists():
        raise HTTPException(503, "vault not mounted")
    p = await db.pool()
    async with p.acquire() as c:
        await c.fetchval("SELECT 1")
        metrics.DEP_UP.labels(dep="postgres").set(1)
        metrics.DEP_UP.labels(dep="vault").set(1)
    return {"ok": True}


@app.get("/metrics")
async def prom():
    p = await db.pool()
    async with p.acquire() as c:
        metrics.CHUNKS.set(await c.fetchval("SELECT count(*) FROM chunks"))
        metrics.DOCS.set(await c.fetchval("SELECT count(*) FROM documents"))
    return PlainTextResponse(generate_latest(), media_type="text/plain; version=0.0.4")


async def _qvec(q: str):
    if not EMBED_URL:
        return None
    try:
        async with httpx.AsyncClient() as cl:
            v = await scanner.embed_batch(cl, [q])
        return v[0]
    except Exception:
        metrics.DEP_UP.labels(dep="embeddings").set(0)
        return None                       # lexical leg still answers


@app.get("/search", dependencies=[Depends(read_auth)])
async def search(q: str, node: str | None = None,
                 tags: str | None = None, after: str | None = None,
                 before: str | None = None, k: int = Query(10, le=50)):
    metrics.SEARCHES.inc()
    t0 = time.monotonic()
    qvec = await _qvec(q)
    p = await db.pool()
    async with p.acquire() as c:
        rows = await db.hybrid_search(
            c, qvec=qvec, qtext=q, node=node,
            tags=tags.split(",") if tags else None,
            after=after, before=before, k=k)
    metrics.SEARCH_LAT.observe(time.monotonic() - t0)
    return {"query": q, "dense": qvec is not None,
            "hits": [dict(r) for r in rows]}


@app.get("/context/{message_uuid}", dependencies=[Depends(read_auth)])
async def context(message_uuid: str, radius: int = Query(3, le=20)):
    """Surrounding messages, read from the markdown itself — the DB locates,
    the vault answers."""
    p = await db.pool()
    async with p.acquire() as c:
        row = await c.fetchrow(
            "SELECT d.path FROM chunks ch JOIN documents d ON d.id=ch.document_id "
            "WHERE ch.message_uuid=$1 LIMIT 1", message_uuid)
    if not row:
        raise HTTPException(404, "unknown message uuid")
    f = Path(VAULT_ROOT, row["path"])
    text = f.read_text(encoding="utf-8-sig", errors="replace")
    m = vaultio.FM_RE.match(text)
    body = text[m.end():] if m else text
    marks = list(vaultio.MSG_RE.finditer(body))
    idx = next((i for i, mk in enumerate(marks) if mk.group(1) == message_uuid), None)
    if idx is None:
        raise HTTPException(404, "uuid not in file (stale index?)")
    lo, hi = max(0, idx - radius), min(len(marks), idx + radius + 1)
    end = marks[hi].start() if hi < len(marks) else len(body)
    return {"path": row["path"], "message_uuid": message_uuid,
            "window": body[marks[lo].start():end]}


@app.post("/scan", dependencies=[Depends(admin_auth)])
async def scan_now():
    n = await scanner.scan_once()
    e = await scanner.embed_drain()
    return {"reindexed": n, "embedded": e}
