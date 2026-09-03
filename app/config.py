"""AgentMemory config — all knobs via env, per spec. Nothing hardcoded."""
import os

VAULT_ROOT = os.environ.get("VAULT_ROOT", "/vault")
PG_DSN = os.environ["PG_DSN"]                      # required
EMBED_URL = os.environ.get("EMBED_URL", "")        # OpenAI-compat embeddings base
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embedding")  # model name sent to /v1/embeddings
# Chat endpoint for the filing agent — any OpenAI-compatible server.
# (QWEN_* names accepted as deprecated aliases; MODEL_API_KEY as a shared-key
# fallback when both services sit behind one provider.)
CHAT_URL = os.environ.get("CHAT_URL") or os.environ.get("QWEN_URL", "")
CHAT_MODEL = (os.environ.get("CHAT_MODEL") or os.environ.get("QWEN_MODEL")
              or "default")
EMBED_API_KEY = (os.environ.get("EMBED_API_KEY")
                 or os.environ.get("MODEL_API_KEY", ""))
CHAT_API_KEY = (os.environ.get("CHAT_API_KEY")
                or os.environ.get("MODEL_API_KEY", ""))
READ_TOKEN = os.environ.get("READ_TOKEN", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

SCAN_INTERVAL_S = int(os.environ.get("SCAN_INTERVAL_S", "300"))
IMPORT_INTERVAL_S = int(os.environ.get("IMPORT_INTERVAL_S", "120"))
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "16"))
EMBED_MAX_CHARS = int(os.environ.get("EMBED_MAX_CHARS", "6000"))
EMBED_DIM = 1024                                   # BGE-M3 dense

FILING_MODE = os.environ.get("FILING_MODE", "propose")  # propose | auto
FILING_CONFIDENCE_MIN = float(os.environ.get("FILING_CONFIDENCE_MIN", "0.85"))
# Auto mode only: node to file below-floor items into (a catch-all like a
# "One-Offs" folder). Empty = leave them pending in .staging for a human.
FILING_LOW_CONF_NODE = os.environ.get("FILING_LOW_CONF_NODE", "")
FILING_INTERVAL_S = int(os.environ.get("FILING_INTERVAL_S", "600"))
FILING_BATCH = int(os.environ.get("FILING_BATCH", "10"))

SKIP_DIRS = {".obsidian", ".imports", ".tools"}    # .staging IS scanned (unfiled searchable)
