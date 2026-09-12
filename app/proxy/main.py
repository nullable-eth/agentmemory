"""Capture proxy entrypoint — `uvicorn proxy.main:app`.

Every path is forwarded. /v1/chat/completions is additionally teed into the
vault; everything else passes through and is counted, so if something ever
starts generating on an endpoint with no capture adapter it shows up in
capture_uncaptured_total rather than being silently missed.

The vault never touches the request path. Capture happens after the last byte
has already reached the client, as one non-blocking put onto a bounded queue;
everything past that point can fail freely without the caller noticing.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import generate_latest

from . import config, forward, metrics, normalize, sse, store
from .writer import Writer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("capture")

CHAT_PATH = "/v1/chat/completions"
# Generation endpoints with no capture adapter. Nothing here uses them; the
# counter exists so that stays true observably rather than by assumption.
UNADAPTED = {"/v1/completions", "/completions", "/completion", "/infill"}

writer = Writer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = forward.client()
    consume = asyncio.create_task(writer.consume())
    sweep = asyncio.create_task(writer.sweep())
    log.info("capture: proxying %s -> %s, vault %s/%s",
             config.PORT, config.UPSTREAM, config.VAULT_ROOT, config.CAPTURE_DIR)
    try:
        yield
    finally:
        sweep.cancel()
        await writer.drain()          # needs consume alive to drain the queue
        consume.cancel()
        await app.state.client.aclose()


app = FastAPI(title="capture-proxy", lifespan=lifespan)


# ------------------------------------------------------------- own surface
@app.get("/__capture/healthz")
async def healthz():
    return {"ok": True}


@app.get("/__capture/metrics")
async def prom():
    return PlainTextResponse(generate_latest(),
                             media_type="text/plain; version=0.0.4")


@app.get("/__capture/status")
async def status():
    convs = writer.store.convs
    return {"tracked": len(convs),
            "open": sum(1 for c in convs.values() if not c.flushed),
            "dirty": sum(1 for c in convs.values() if c.dirty),
            "queued": writer.queue.qsize()}


# ------------------------------------------------------------------ capture
def _record(parsed, acc, buf, started, headers, truncated) -> None:
    """Build one capture record and hand it to the writer. Never raises."""
    try:
        msgs = normalize.request_messages(parsed)
        if not msgs:
            return
        reply = None
        if acc is not None:
            if not acc.empty():
                reply = acc.message(truncated=truncated)
        elif buf is not None:
            try:
                obj = json.loads(bytes(buf).decode("utf-8", "replace"))
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                reply = normalize.response_message(obj)
                if reply is not None:
                    if isinstance(obj.get("usage"), dict):
                        reply.extra["usage"] = obj["usage"]
                    reply.truncated = truncated
        if truncated:
            metrics.TRUNCATED.inc()
        writer.submit({
            "messages": msgs,
            "reply": reply,
            "ts": started,
            "reply_ts": store.now_iso(),
            "client_id": (headers.get(config.HDR_CONV_ID) or "").strip(),
            "title_hint": (headers.get(config.HDR_TITLE) or "").strip(),
        })
    except Exception:
        log.exception("capture: record failed")
        metrics.DROPPED.labels(reason="record_error").inc()


# -------------------------------------------------------------- the proxy
@app.api_route("/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD",
                        "OPTIONS"])
async def proxy(path: str, request: Request):
    body = await request.body()
    endpoint = "/" + path
    client: httpx.AsyncClient = request.app.state.client

    parsed = None
    if endpoint == CHAT_PATH and request.method == "POST":
        client_name = (request.headers.get(config.HDR_CLIENT) or "").strip()
        if client_name in config.NOLOG_CLIENTS:
            # Identified machine traffic: proxied exactly like anything else,
            # just not written down. Counted so it is still accounted for.
            metrics.SUPPRESSED.labels(client=client_name).inc()
        else:
            try:
                candidate = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                candidate = None
            if isinstance(candidate, dict) and candidate.get("messages"):
                parsed = candidate
    elif endpoint in UNADAPTED:
        metrics.UNCAPTURED.labels(endpoint=endpoint).inc()

    upstream = client.build_request(
        request.method, forward.url_for(path, request.url.query),
        headers=forward.upstream_headers(request.headers), content=body)
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as e:
        metrics.UPSTREAM_ERRORS.labels(endpoint=endpoint).inc()
        log.warning("capture: upstream error on %s: %s", endpoint, e)
        return JSONResponse(
            {"error": {"message": f"upstream unreachable: {e}",
                       "type": "proxy_error"}}, status_code=502)

    streamed = "text/event-stream" in (resp.headers.get("content-type") or "")
    metrics.REQUESTS.labels(endpoint=endpoint,
                            streamed="true" if streamed else "false").inc()

    capture = parsed is not None and resp.status_code < 400
    acc = sse.ChatAccumulator() if (capture and streamed) else None
    buf = bytearray() if (capture and not streamed) else None
    started = store.now_iso()

    async def tee():
        truncated = True
        try:
            async for chunk in resp.aiter_raw():
                if acc is not None:
                    acc.feed(chunk)
                elif buf is not None and len(buf) < config.MAX_BODY:
                    buf.extend(chunk)
                yield chunk
            truncated = False
        finally:
            await resp.aclose()
            if capture:
                _record(parsed, acc, buf, started, request.headers, truncated)

    return StreamingResponse(tee(), status_code=resp.status_code,
                             headers=forward.downstream_headers(resp.headers))
