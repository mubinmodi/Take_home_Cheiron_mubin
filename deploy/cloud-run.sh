#!/usr/bin/env bash
# Deploy the hosted service to Google Cloud Run (docs/hosted-deployment.md).
#
# Before the first run:
#   - a Google Cloud project with billing, and the gcloud CLI logged in (gcloud auth login)
#   - an Upstash Redis database (its rediss:// URL) and a Neon Postgres database (its connection string)
#   - optional: a Langfuse project for traces (public and secret key)
#
# Usage:
#   PROJECT=my-project deploy/cloud-run.sh
#
# Secrets live in Secret Manager. A missing secret is asked for (input hidden) and created; to replace
# one, export it before running (e.g. API_KEYS=alice:...). Values are never printed.
set -euo pipefail

PROJECT=${PROJECT:?set PROJECT to your Google Cloud project ID}
REGION=${REGION:-us-central1}
SERVICE=${SERVICE:-clinical-trials-viz}
SECRETS=(OPENAI_API_KEY ANTHROPIC_API_KEY REDIS_URL DATABASE_URL API_KEYS)

gcloud config set project "$PROJECT" --quiet
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iam.googleapis.com --quiet
# The service runs as its own account, which may read only its secrets.
RUNTIME_ACCOUNT="$SERVICE@$PROJECT.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$RUNTIME_ACCOUNT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SERVICE" --display-name="$SERVICE (Cloud Run)" --quiet

put_secret() {  # create a secret, or add a version when a new value is exported
  local name=$1 value=${!1:-}
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    if [ -n "$value" ]; then
      printf %s "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
    fi
  else
    [ -z "$value" ] && { read -rsp "$name: " value; echo; }
    printf %s "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUNTIME_ACCOUNT" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  echo "secret $name ready"
}

ENV_VARS="RUN_DEADLINE_SECONDS=30,USER_QUERIES_PER_HOUR=30,LOG_LEVEL=INFO"
if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ]; then
  # Traces go to Langfuse over OTLP; the auth header is a secret too.
  # OTLP header values are URL-encoded: %20 is the space after "Basic".
  OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20$(printf %s "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" | base64 | tr -d '\n')"
  export OTEL_EXPORTER_OTLP_HEADERS
  SECRETS+=(OTEL_EXPORTER_OTLP_HEADERS)
  ENV_VARS+=",OTEL_EXPORTER=otlp,OTEL_EXPORTER_OTLP_ENDPOINT=${LANGFUSE_OTLP_ENDPOINT:-https://cloud.langfuse.com/api/public/otel}"
fi

for name in "${SECRETS[@]}"; do put_secret "$name"; done
SECRET_FLAGS=$(for name in "${SECRETS[@]}"; do printf '%s=%s:latest,' "$name" "$name"; done | sed 's/,$//')

# Requests get 60 s at the platform; the service itself stops a Run at 30 s with a clean run_timeout.
# Anyone may load the page and read runs; asking (POST /v1/query) needs an X-API-Key from API_KEYS.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --quiet \
  --service-account "$RUNTIME_ACCOUNT" --allow-unauthenticated \
  --cpu 1 --memory 1Gi --concurrency 20 --timeout 60 \
  --min-instances 0 --max-instances 3 \
  --set-env-vars "$ENV_VARS" --set-secrets "$SECRET_FLAGS"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "Deployed: $URL"
echo "Smoke test: deploy/smoke-test.sh $URL   (asks for an API key)"
