FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py pull.py ./

# All runtime state (corpus/, tokens, model cache, logs) lives in /data,
# which is meant to be a volume. The code itself stays in the image.
ENV RAG_SHARE_DIR=/data \
    RAG_SHARE_BIND_HOSTS=0.0.0.0 \
    RAG_SHARE_PORT=8790 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8790

CMD ["python", "server.py"]
