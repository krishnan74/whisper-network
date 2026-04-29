FROM python:3.11-slim

# Install curl (used by start.sh health check) and openssl (key generation)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl openssl \
    && rm -rf /var/lib/apt/lists/*

# Copy AXL pre-built binary
COPY axl/node /axl/node
RUN chmod +x /axl/node

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application source
COPY whisper/   /app/whisper/
COPY demo/      /app/demo/
COPY comparison/ /app/comparison/
COPY start.sh   /app/start.sh

WORKDIR /app

# Volumes expected at runtime:
#   /keys/    — persistent ed25519 key (generated on first start if absent)
#   /config/  — AXL node-config-N.json
#   /shards/  — document shard text files
#   /data/    — persistent ledger.json

ENTRYPOINT ["/app/start.sh"]
