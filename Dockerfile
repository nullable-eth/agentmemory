FROM python:3.12-slim
WORKDIR /srv/app
RUN pip install --no-cache-dir \
    fastapi==0.115.* uvicorn==0.30.* asyncpg==0.29.* httpx==0.27.* \
    prometheus-client==0.20.*
COPY app/ /srv/app/
EXPOSE 8080
USER 1000:1000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
