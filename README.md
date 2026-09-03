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
- The vault mounted read-write at `VAULT_ROOT`.

## Configuration (all via environment)

| Variable | Default | Purpose |
|---|---|---|
| `PG_DSN` | *(required)* | `postgresql://user:pass@host:5432/db` |
| `VAULT_ROOT` | `/vault` | Mounted vault path |
| `EMBED_URL` | *(empty)* | OpenAI-compatible embeddings base URL; empty disables the dense leg |
| `EMBED_MODEL` | `embedding` | Model name sent in embeddings requests (llama.cpp ignores it; multi-model servers need the real name) |
| `QWEN_URL` | *(empty)* | OpenAI-compatible chat base URL (filing agent) |
| `MODEL_API_KEY` | *(empty)* | Bearer key sent to both model endpoints |
| `READ_TOKEN` | *(empty)* | Bearer token for `/search` `/context`; empty = open |
| `ADMIN_TOKEN` | *(empty)* | Bearer token for `/scan` |
| `SCAN_INTERVAL_S` | `300` | Vault sweep period |
| `IMPORT_INTERVAL_S` | `120` | `.imports/` inbox poll period (export-zip watcher) |
| `EMBED_BATCH` | `16` | Chunks per embeddings request |
| `EMBED_MAX_CHARS` | `6000` | Chunk split threshold |
| `FILING_MODE` | `propose` | `propose` or `auto` — whether filing proposals apply themselves |
| `FILING_CONFIDENCE_MIN` | `0.85` | Auto-apply confidence floor |

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
