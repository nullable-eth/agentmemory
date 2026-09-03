"""AgentMemory API — search over the vault; loops run in-process.

Surfaces:
  REST: /search /context /scan /healthz /readyz /metrics
  MCP:  /mcp (streamable HTTP) — tools: search_memory, get_context

Auth: READ_TOKEN for search/context/MCP, ADMIN_TOKEN for scan. Readiness
gates on PG + vault mount only: search must survive model outages.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server.fastmcp import FastMCP
from prometheus_client import generate_latest

import db
import metrics
import scanner
import vaultio
from config import (ADMIN_TOKEN, EMBED_URL, READ_TOKEN, VAULT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ----------------------------------------------------------------- shared impl
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


async def do_search(q: str, node=None, tags=None, after=None, before=None, k=10):
    metrics.SEARCHES.inc()
    t0 = time.monotonic()
    qvec = await _qvec(q)
    p = await db.pool()
    async with p.acquire() as c:
        rows = await db.hybrid_search(
            c, qvec=qvec, qtext=q, node=node, tags=tags,
            after=after, before=before, k=k)
    metrics.SEARCH_LAT.observe(time.monotonic() - t0)
    return {"query": q, "dense": qvec is not None,
            "hits": [{**dict(r), "date": str(r["date"]) if r["date"] else None,
                      "created_at": str(r["created_at"]) if r["created_at"] else None,
                      "score": float(r["score"])} for r in rows]}


async def do_context(message_uuid: str, radius: int = 3):
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


# ------------------------------------------------------------------------- MCP
mcp = FastMCP("agentmemory", stateless_http=True, streamable_http_path="/")


@mcp.tool()
async def search_memory(query: str, node: str | None = None,
                        tags: str | None = None, after: str | None = None,
                        before: str | None = None, k: int = 10) -> dict:
    """Hybrid semantic + exact-keyword search over the entire conversation
    archive (all chat transcripts, project files, memory dossiers). Use for
    any question about past conversations, decisions, designs, or facts —
    both natural-language questions and exact identifiers/config keys work.
    Optional filters: node (top-level topic folder), tags (comma-separated),
    after/before (YYYY-MM-DD). Returns scored hits with message_uuid for
    follow-up via get_context."""
    return await do_search(query, node=node,
                           tags=tags.split(",") if tags else None,
                           after=after, before=before, k=min(k, 50))


@mcp.tool()
async def get_context(message_uuid: str, radius: int = 3) -> dict:
    """Fetch the surrounding conversation window for a search hit, read
    verbatim from the archive. radius = messages on each side (max 20)."""
    return await do_context(message_uuid, min(radius, 20))


# --------------------------------------------------------------------- FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.pool()
    task = asyncio.create_task(scanner.scan_loop())
    async with mcp.session_manager.run():
        yield
    task.cancel()


app = FastAPI(title="agentmemory", lifespan=lifespan)
app.mount("/mcp", mcp.streamable_http_app())


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp") and READ_TOKEN:
        if request.headers.get("authorization") != f"Bearer {READ_TOKEN}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


def _auth(token: str):
    async def dep(authorization: str = Header(default="")):
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401)
    return dep


read_auth, admin_auth = _auth(READ_TOKEN), _auth(ADMIN_TOKEN)


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


@app.get("/search", dependencies=[Depends(read_auth)])
async def search(q: str, node: str | None = None, tags: str | None = None,
                 after: str | None = None, before: str | None = None,
                 k: int = Query(10, le=50)):
    return await do_search(q, node=node, tags=tags.split(",") if tags else None,
                           after=after, before=before, k=k)


@app.get("/context/{message_uuid}", dependencies=[Depends(read_auth)])
async def context(message_uuid: str, radius: int = Query(3, le=20)):
    return await do_context(message_uuid, radius)


@app.post("/scan", dependencies=[Depends(admin_auth)])
async def scan_now():
    n = await scanner.scan_once()
    e = await scanner.embed_drain()
    return {"reindexed": n, "embedded": e}
