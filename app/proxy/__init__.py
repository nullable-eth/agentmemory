"""Capture proxy — an OpenAI-API-transparent tee in front of llama.cpp.

Second entrypoint of the same image (see Dockerfile). Runs as a sidecar in
the qwen-coder pod: the Service targets this, this forwards to llama.cpp on
localhost, and every conversation that passes through is archived into the
agentmemory vault as markdown the scanner already knows how to chunk.

Nothing in here may import app/config.py or app/db.py — the sidecar has no
database, and config.py does os.environ["PG_DSN"] at import time.
"""
