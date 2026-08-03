FROM python:3.11-slim-bookworm

# ffmpeg + a font for burned-in subtitles
# Retries + IPv4-only work around transient DNS/network flakiness on some cloud build hosts
RUN apt-get update -o Acquire::Retries=5 -o Acquire::ForceIPv4=true \
    && apt-get install -y --no-install-recommends \
       ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY static static

ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
