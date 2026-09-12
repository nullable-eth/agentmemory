# agentmemory

A self-contained service that turns an Obsidian-style vault of AI chat
transcripts into an agent-queryable memory: it watches the vault, chunks and
embeds every conversation, and serves hybrid semantic + full-text search over
the whole corpus via a small HTTP API.

Built for vaults produced from AI assistant data exports (e.g. Claude), where
each transcript message carries an invisible `<!-- msg:uuid -->` marker — but
generic: everything is configured by environment variables, nothing is
hardcoded, and the markdown vault remains the sole source of truth. The
database holds derived data only and can be dropped and rebuilt at any time.

## What it does

- **Scan** — polls the vault for new/changed markdown (polling, not inotify,
  so network mounts like CIFS/NFS work), parses frontmatter, chunks
  transcripts on message-uuid boundaries. Chunk identity is
  `(message_uuid, ordinal)`, so re-imports re-embed only what changed.
- **Embed** — batches chunks to any OpenAI-compatible `/v1/embeddings`
  endpoint (designed around BGE-M3, 1024-dim dense). Backlog state lives in
  the DB; the drain is resumable and survives restarts.
- **Search** — `GET /search`: reciprocal-rank fusion of dense (pgvector HNSW,
  cosine) and lexical (Postgres `websearch_to_tsquery`) rank lists, with
  node/tag/date filters. Degrades gracefully to lexical-only when the
  embedding endpoint is down. Exact-token recall (identifiers, config keys)
  is a first-class goal — that's what the lexical leg is for.
- **Context** — `GET /context/{message_uuid}`: the surrounding conversation
  window, read from the markdown itself at request time.
- **Import** (vendored pipeline) — `app/importer/` contains the export
  unpacker/merger: assistant export zips in, complete verbatim markdown out,
  multi-part and per-category exports merged, idempotent re-runs.
- **Observe** — Prometheus `/metrics`: scan staleness, embed backlog,
  unfiled-item gauges, dependency reachability, search latency. `/healthz`,
  `/readyz` (DB + vault mount only — model outages never gate readiness).

## Requirements

- PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector)
  extension available (`CREATE EXTENSION vector` is run by the schema).
- An OpenAI-compatible embeddings endpoint (optional — lexical search works
  without it).
- Optionally, an OpenAI-compatible chat endpoint for filing proposals.
- The vault mounted read-write at `VAULT_ROOT`.

## Reference stack (what this was built and tested against)

Any OpenAI-compatible servers work; this is the known-good combination:

- **Embeddings: BGE-M3** (GGUF, FP16) served by `llama.cpp` (`--embeddings`).
  1024-dim dense output — matches the schema's `vector(1024)`; change both
  together if you swap models. Hard-won sizing note: llama.cpp requires each
  embedding input to fit one physical batch, so run with
  `--ctx-size = --batch-size = --ubatch-size` ≥ your longest chunk in tokens
  (chunks here cap at `EMBED_MAX_CHARS`=6000 chars ≈ ~2.7k tokens worst case;
  we run 4096). Undersized batch → HTTP 500 `input is too large to process`;
  the drain then truncates or quarantines rather than stalling, but sizing it
  right is better.
- **Filing LLM: a Qwen3-class instruct model (~27B)** on `llama.cpp`'s
  OpenAI-compatible server. Anything that reliably returns strict JSON at low
  temperature works; smaller models mostly cost filing precision, and
  `FILING_MODE=propose` (the default) keeps a human approving until yours
  earns `auto`.
- **Postgres 16/17 + pgvector ≥ 0.8.** We run it on Kubernetes via the
  CloudNativePG operator using the `tensorchord/vchord-postgres` image
  (pgvector included), one dedicated database owned by a dedicated role — but
  any Postgres with pgvector available satisfies the service; `schema.sql`
  self-applies idempotently at startup, HNSW + GIN indexes included.

## Quickstart: a searchable RAG from your own Claude export

1. Request a data export (Claude → Settings → Privacy → Export data) and
   download the zip(s) — new-style exports are a manifest of one-time,
   browser-gated links, so download them in your browser.
2. Create your vault folder and drop the zips in `<vault>/.imports/`.
3. Render them: `VAULT_ROOT=<vault> python app/importer/import_export.py`
   — every conversation becomes verbatim markdown under `<vault>/.staging/`,
   each message behind a `<!-- msg:uuid -->` marker (the chunk-identity
   contract everything else builds on). Re-runs are idempotent and merges are
   by message uuid, so repeat exports only add.
4. Run the service (see **Run** below) with the vault mounted and `PG_DSN`
   set. First scan indexes everything; embeddings backfill in the background;
   `/search` and the `/mcp` tools answer from then on. Organizing files out
   of `.staging/` into topic folders ("nodes") is optional — search works
   either way; nodes add filtering and the filing-proposal workflow.

## Configuration (all via environment)

| Variable | Default | Purpose |
|---|---|---|
| `PG_DSN` | *(required)* | `postgresql://user:pass@host:5432/db` |
| `VAULT_ROOT` | `/vault` | Mounted vault path |
| `EMBED_URL` | *(empty)* | OpenAI-compatible embeddings base URL; empty disables the dense leg |
| `EMBED_MODEL` | `embedding` | Model name sent in embeddings requests (llama.cpp ignores it; multi-model servers need the real name) |
| `CHAT_URL` | *(empty)* | OpenAI-compatible chat base URL for the filing agent; empty disables filing |
| `CHAT_MODEL` | `default` | Model name sent to the chat endpoint |
| `CHAT_API_KEY` | *(empty)* | Bearer key for the chat endpoint |
| `EMBED_API_KEY` | *(empty)* | Bearer key for the embeddings endpoint |
| `MODEL_API_KEY` | *(empty)* | Shared-key fallback for both when one provider serves them |
| `READ_TOKEN` | *(empty)* | Bearer token for `/search` `/context`; empty = open |
| `ADMIN_TOKEN` | *(empty)* | Bearer token for `/scan` |
| `SCAN_INTERVAL_S` | `300` | Vault sweep period |
| `IMPORT_INTERVAL_S` | `120` | `.imports/` inbox poll period (export-zip watcher) |
| `EMBED_BATCH` | `16` | Chunks per embeddings request |
| `EMBED_MAX_CHARS` | `6000` | Chunk split threshold |
| `FILING_MODE` | `propose` | `propose` or `auto` — whether filing proposals apply themselves |
| `FILING_CONFIDENCE_MIN` | `0.85` | Auto-apply confidence floor |
| `FILING_LOW_CONF_NODE` | *(empty)* | Auto mode: catch-all node for below-floor items (original proposal kept in the rationale); empty leaves them pending |
| `FILING_INTERVAL_S` | `600` | Filing-proposal cycle period |
| `FILING_BATCH` | `10` | Max files proposed per cycle |

## Capture proxy (second entrypoint)

Same image, different command: an OpenAI-API-transparent tee that sits in
front of any llama.cpp/OpenAI-compatible server and archives every
conversation that crosses it into the vault as markdown the scanner already
knows how to chunk. Zero client changes — point the Service at the proxy and
let it forward to the model on localhost.

```
docker run -p 8010:8010 \
  -e CAPTURE_UPSTREAM=http://127.0.0.1:8000 -e VAULT_ROOT=/vault \
  -v /path/to/vault:/vault \
  ghcr.io/nullable-eth/agentmemory:latest \
  uvicorn proxy.main:app --host 0.0.0.0 --port 8010
```

It holds no API key: `Authorization` is forwarded verbatim, so the upstream
keeps enforcing its own auth exactly as before. Every path is proxied;
`/v1/chat/completions` is additionally captured, and anything else that could
generate is counted in `capture_uncaptured_total`. Its own surface lives under
`/__capture/` (`healthz`, `metrics`, `status`).

**Conversations are buffered, not streamed to disk.** A conversation lives in
`<capture dir>/.index/<uuid>.json` until it has been quiet for
`CAPTURE_IDLE_S`, and only then becomes markdown. Both the scanner and the
filing agent glob `*.md`, so nothing in `.index/` is ever indexed, embedded or
filed. That is deliberate: without it, an agent that searches memory mid-run
retrieves its own half-formed reasoning from ten minutes ago and cannot tell
it apart from an archived conclusion.

Message uuids are `uuid5(conversation_uuid, content_key)`, so re-sent history
converges onto the same uuids and `replace_chunks` carries existing embeddings
forward — reopening a conversation re-embeds only what changed. Conversation
uuids are random: two chats that open with identical text are two chats.

**One narrow exception to capturing everything.** A client may identify
itself with `X-Capture-Client: <name>`, and if that name is in
`CAPTURE_NOLOG_CLIENTS` the exchange is proxied normally but no transcript is
written. It exists for the filing agent, whose own calls to `CHAT_URL` now
return through the proxy: their system prompt is `CLAUDE.md` plus every
`Scope.md`, their user prompt quotes a file already in the vault, and their
verdict is already persisted in `filing_proposals`. Archiving them would
duplicate the vault into itself on every cycle, and — because a transcript in
`.staging` is a filing candidate — would hand the filing agent one new
candidate for every candidate it consumed, forever.

It is an allowlist, not a boolean opt-out: a client cannot suppress itself by
inventing a name, the accepted names are configured here rather than by the
caller, and an unrecognised value is captured and indexed like anything else.
Suppressed exchanges still increment `capture_suppressed_total{client}`, so
they are unarchived but not unaccounted for. This is not a security boundary
— anything that can reach the endpoint can send a known name — but the
default is inclusive, so nothing falls out of the archive by omission.

**Context compaction.** When a request would overflow the model's window, the
proxy summarises the middle of the conversation and forwards
`[system…, state summary, recent tail]` instead, so no client can run the
model out of context regardless of how it manages history. Because the vault
already holds the conversation verbatim, this costs nothing archivally — the
transcript stays complete, only the forwarded prompt shrinks, and the reply
records `context_compacted` so the archive doesn't imply the model saw
everything. Token counts are exact, via the server's own `/apply-template`
and `/tokenize`; the window size comes from `/props`.

The proxy holds no API key and does not start holding one for this: its
`/props`, `/tokenize` and summarising calls reuse the **caller's**
`Authorization` header, so they are made on behalf of someone already
entitled to use the model. No header, no compaction. Every failure path —
tokenizer unavailable, summariser refusing, a tail that is itself over budget
— forwards the request unchanged rather than mangling it, and a cut is never
placed where it would separate an assistant `tool_calls` from its `tool`
result. Summaries are cached by the span they cover, so a client that resends
its whole history every turn pays for one summary rather than one per turn.

| Variable | Default | Purpose |
|---|---|---|
| `CAPTURE_UPSTREAM` | `http://127.0.0.1:8000` | Where to forward |
| `CAPTURE_PORT` | `8010` | Informational; uvicorn owns the real bind |
| `CAPTURE_DIR` | `.staging/Chats/Live Capture` | Vault-relative transcript folder |
| `CAPTURE_IDLE_S` | `1800` | Quiet period before a conversation is written |
| `CAPTURE_MAX_OPEN_S` | `43200` | Safety valve for a conversation that never goes quiet |
| `CAPTURE_REOPEN_S` | `604800` | How long a written conversation stays reopenable |
| `CAPTURE_SWEEP_S` | `60` | Flush/retry tick |
| `CAPTURE_QUEUE_MAX` | `256` | Pending capture records; oldest dropped past this |
| `CAPTURE_MAX_CONVERSATIONS` | `500` | In-memory ceiling, hit only during a long vault outage |
| `CAPTURE_MAX_BODY` | `67108864` | Cap on the copy kept of a non-streamed response |
| `CAPTURE_TITLE_MAX` | `60` | Title length taken from the first user message |
| `CAPTURE_CONNECT_TIMEOUT_S` | `5` | Upstream connect timeout; there is no read timeout |
| `CAPTURE_NOLOG_CLIENTS` | `agentmemory-filing` | Comma-separated `X-Capture-Client` names whose exchanges are proxied but never written |
| `CAPTURE_COMPACT` | `1` | Summarise the middle of a conversation that would overflow the context |
| `CAPTURE_COMPACT_AT` | `0.75` | Fraction of `n_ctx` a prompt may occupy before compaction |
| `CAPTURE_COMPACT_KEEP_TAIL` | `8` | Recent messages kept verbatim |
| `CAPTURE_COMPACT_RESERVE` | `8192` | Generation headroom assumed when a request sets no `max_tokens` |
| `CAPTURE_COMPACT_SUMMARY_TOKENS` | `2000` | Cap on the state summary |
| `CAPTURE_COMPACT_CACHE_MAX` | `256` | Cached summaries, keyed by the span they cover |

Clients may also send `X-Capture-Conversation-Id` (name your own conversation
— an alert-run id, say) and `X-Capture-Title`. Neither affects whether an
exchange is captured.

Smoke test: `python tests/test_capture.py` runs a fake upstream and the real
proxy against a scratch vault and asserts on both the rendered markdown and
what `vaultio.chunk_transcript` makes of it.

## Run

```
docker run -p 8080:8080 \
  -e PG_DSN=postgresql://... -e EMBED_URL=http://embedder:8002 \
  -v /path/to/vault:/vault \
  ghcr.io/nullable-eth/agentmemory:latest
```

Schema applies itself at startup (idempotent). First scan indexes the whole
vault; embeddings backfill in the background.

## Release

Push to `main` → `:latest` + `:sha-…`. Tag `vX.Y.Z` → `:X.Y.Z`.
