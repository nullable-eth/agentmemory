FROM python:3.12-slim
# eMudhra CA compromise (20 Oct 26): eight eMudhra intermediate keys are
# derivable from their certificates. Take the distro's fixed ca-certificates
# even when the base image predates it.
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/app
# pydantic capped at 2.13: mcp 1.12.x imports
# pydantic._internal._typing_extra.eval_type_backport, which pydantic 2.14
# removed. The 22 Oct rebuild (eMudhra) re-resolved the otherwise unpinned
# set and pulled pydantic 2.14.1; the image then died at import
# (TargetDown, ai/agentmemory on whitehorse). Lift the cap with a 2.14-clean
# mcp.
RUN pip install --no-cache-dir \
    fastapi==0.115.* uvicorn==0.30.* asyncpg==0.29.* httpx==0.27.* \
    prometheus-client==0.20.* \
    'pydantic==2.13.*' \
    'mcp>=1.9,<2'
COPY app/ /srv/app/
EXPOSE 8080
# certifi >= 2026.10.19 drops the compromised roots; requests/httpx use it,
# not the OS store.
RUN pip install --no-cache-dir -U "certifi>=2026.10.19"

USER 1000:1000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
