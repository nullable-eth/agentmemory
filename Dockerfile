FROM python:3.12-slim
# eMudhra CA compromise (20 Oct 26): eight eMudhra intermediate keys are
# derivable from their certificates. Take the distro's fixed ca-certificates
# even when the base image predates it.
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/app
RUN pip install --no-cache-dir \
    fastapi==0.115.* uvicorn==0.30.* asyncpg==0.29.* httpx==0.27.* \
    prometheus-client==0.20.* \
    mcp==1.12.4 pydantic==2.13.5 pydantic-core==2.46.5
# mcp and pydantic pinned to the last set that ran (sha-29ca1dc). A rebuild on
# 22 Oct 26 floated to a newer pydantic whose internals mcp 1.12 imports
# (pydantic._internal._typing_extra.eval_type_backport, removed), and the pod
# crash-looped on import. Bump them together, deliberately.
COPY app/ /srv/app/
EXPOSE 8080
# certifi >= 2026.10.19 drops the compromised roots; requests/httpx use it,
# not the OS store.
RUN pip install --no-cache-dir -U "certifi>=2026.10.19"

USER 1000:1000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
