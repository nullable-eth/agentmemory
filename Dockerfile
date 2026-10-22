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
    'mcp>=1.9,<2'
COPY app/ /srv/app/
EXPOSE 8080
# certifi >= 2026.10.19 drops the compromised roots; requests/httpx use it,
# not the OS store.
RUN pip install --no-cache-dir -U "certifi>=2026.10.19"

USER 1000:1000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
