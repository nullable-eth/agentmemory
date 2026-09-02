-- AgentMemory schema (docs/agentmemory-spec.md). Idempotent; applied by the
-- service at startup. DB is derived data only — the vault markdown is truth.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id           bigserial PRIMARY KEY,
  path         text NOT NULL UNIQUE,          -- vault-relative, fwd slashes
  source_uuid  text,
  node         text,
  title        text,
  date         date,
  doc_type     text,
  tags         text[] NOT NULL DEFAULT '{}',
  content_hash text NOT NULL,
  mtime        double precision NOT NULL,
  indexed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
  id           bigserial PRIMARY KEY,
  document_id  bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  message_uuid text,                          -- null for non-transcript notes
  ordinal      int NOT NULL DEFAULT 0,
  sender       text,
  created_at   timestamptz,
  text         text NOT NULL,
  tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding    vector(1024),                  -- BGE-M3 dense; null = backlog
  UNIQUE (document_id, message_uuid, ordinal)
);

CREATE TABLE IF NOT EXISTS filing_proposals (
  id            bigserial PRIMARY KEY,
  path          text NOT NULL,
  proposed_node text NOT NULL,
  proposed_tags text[] NOT NULL DEFAULT '{}',
  confidence    real NOT NULL,
  rationale     text,
  status        text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','applied')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  decided_at    timestamptz
);

CREATE TABLE IF NOT EXISTS scan_state (
  path         text PRIMARY KEY,
  mtime        double precision NOT NULL,
  size         bigint NOT NULL,
  content_hash text NOT NULL,
  last_seen    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_tsv_gin  ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_msg_uuid ON chunks (message_uuid);
CREATE INDEX IF NOT EXISTS documents_node_date ON documents (node, date);
-- HNSW built once rows exist; harmless if empty.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks
  USING hnsw (embedding vector_cosine_ops);
