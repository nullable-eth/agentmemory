"""Capture proxy config — all knobs via env, nothing hardcoded.

Deliberately standalone: app/config.py requires PG_DSN at import time and the
sidecar has no business holding a database DSN.
"""
import os

UPSTREAM = os.environ.get("CAPTURE_UPSTREAM", "http://127.0.0.1:8000").rstrip("/")
VAULT_ROOT = os.environ.get("VAULT_ROOT", "/vault")
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", ".staging/Chats/Live Capture")
PORT = int(os.environ.get("CAPTURE_PORT", "8010"))

# A conversation is buffered in the index (invisible to scanner and filing
# agent alike) and materialised as markdown only once it has been quiet this
# long. This is the whole point of the buffer: an agent mid-run must not be
# able to retrieve its own in-flight reasoning back out of RAG and mistake it
# for archived knowledge.
IDLE_S = int(os.environ.get("CAPTURE_IDLE_S", "2700"))            # 45 min
# Safety valve: a conversation that never goes quiet still gets written.
MAX_OPEN_S = int(os.environ.get("CAPTURE_MAX_OPEN_S", "43200"))   # 12 h
SWEEP_S = int(os.environ.get("CAPTURE_SWEEP_S", "60"))
# How long a flushed conversation stays reopenable by a late continuation.
REOPEN_S = int(os.environ.get("CAPTURE_REOPEN_S", "604800"))      # 7 d

# Vault writes are best-effort and must never touch the proxied request. A
# NAS outage drops the oldest records and increments a counter; that is its
# own class of problem and not one this process tries to solve.
QUEUE_MAX = int(os.environ.get("CAPTURE_QUEUE_MAX", "256"))
# Hard ceiling on conversations held in memory. Reached only when the vault
# has been unwritable long enough for unflushed work to pile up.
MAX_CONVERSATIONS = int(os.environ.get("CAPTURE_MAX_CONVERSATIONS", "500"))

# Ceiling on the copy of a non-streamed response body kept for parsing.
MAX_BODY = int(os.environ.get("CAPTURE_MAX_BODY", str(64 << 20)))

TITLE_MAX = int(os.environ.get("CAPTURE_TITLE_MAX", "60"))
# Client hints. Neither can suppress capture — they only name a conversation
# that the client already knows the identity of (cluster-agent's run id).
HDR_CONV_ID = "x-capture-conversation-id"
HDR_TITLE = "x-capture-title"

CONNECT_TIMEOUT_S = float(os.environ.get("CAPTURE_CONNECT_TIMEOUT_S", "5"))
