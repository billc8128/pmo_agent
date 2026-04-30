# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Base tools:
#   - nodejs/npm: claude-agent-sdk ships a Node CLI that the Python SDK
#                 forks under the hood (it's how it talks to the
#                 Anthropic-compatible endpoint).
#   - ca-certificates: HTTPS to Supabase + 火山方舟
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/start.sh

# Railway injects PORT
ENV PORT=8080
EXPOSE 8080

CMD ["bash", "/app/start.sh"]
