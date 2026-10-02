# LODE ingestion service — the production front door for the "Add to LODE" button.
# Build from the REPO ROOT so src/ and scripts/ are in context:
#   az acr build --registry ormwacr01 --image lode-ingest:v1 --file api/Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

# Only what the ingestion path actually needs. No data/, no corpus/ — the asset-alias ledger is
# optional (chain.load_asset_aliases is a no-op when absent) and the corpus is not used here.
COPY src/ ./src/
COPY scripts/add_one_to_lode.py ./scripts/add_one_to_lode.py
COPY api/ ./api/

# Container Apps sets PORT; default for local runs.
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
