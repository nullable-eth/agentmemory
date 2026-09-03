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
