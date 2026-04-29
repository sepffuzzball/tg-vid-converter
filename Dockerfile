FROM python:3.12-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Create temp directory with proper permissions
RUN mkdir -p /tmp/tg-vid && chown appuser:appuser /tmp/tg-vid

# Environment defaults
ENV PORT=8080
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_CHAT_IDS=""
ENV ALLOWED_SUBNET=""
ENV FFMPEG_PATH=ffmpeg

# Run as non-root user
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

CMD ["python", "app.py"]
