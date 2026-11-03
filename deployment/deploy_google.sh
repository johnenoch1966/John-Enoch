#!/usr/bin/env bash
# Deploy the "Refresh research" runner INSIDE GOOGLE CLOUD with Editor-level
# permissions only (no IAM / Workload Identity changes needed):
#
#   Cloud Scheduler (every 10 min) -> Cloud Run Job `adoption-agent-refresh`
#   (python -m adoption_agent.cli poll) -> runs a research cycle when a
#   request is queued in status.json on the research-data branch, then
#   publishes to GitHub with a dedicated, repo-scoped deploy key.
#
# Usage:  PROJECT_ID=ezyai-demos DEPLOY_KEY=~/.ssh/john_enoch_cloudrun ./deployment/deploy_google.sh
# Re-run any time to roll out a new image (idempotent).
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
DEPLOY_KEY="${DEPLOY_KEY:?path to the job's GitHub deploy key (private)}"
REPO="adoption-agent"
JOB="adoption-agent-refresh"
SCHED="adoption-agent-poll"
BUCKET="${PROJECT_ID}-adoption-agent-secrets"
MODEL="${MODEL:-gemini-3.8-flash}"
TAG="$(git rev-parse --short=12 HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/agent:${TAG}"
GCLOUD="${GCLOUD:-gcloud}"
PNUM="$($GCLOUD projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${SA:-${PNUM}-compute@developer.gserviceaccount.com}"

echo "==> Enabling APIs"
$GCLOUD services enable run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com aiplatform.googleapis.com storage.googleapis.com --project "$PROJECT_ID"

echo "==> Artifact Registry"
$GCLOUD artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  $GCLOUD artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
    --description "Adoption research agent images" --project "$PROJECT_ID"

echo "==> Private bucket for the job's GitHub deploy key"
$GCLOUD storage buckets describe "gs://$BUCKET" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  $GCLOUD storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access \
    --public-access-prevention --project "$PROJECT_ID"
$GCLOUD storage cp "$DEPLOY_KEY" "gs://$BUCKET/secrets/github_deploy_key" --project "$PROJECT_ID"

echo "==> Building image $IMAGE (Cloud Build)"
$GCLOUD builds submit --tag "$IMAGE" --project "$PROJECT_ID" --region "$REGION" .

echo "==> Cloud Run Job $JOB"
$GCLOUD run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" --project "$PROJECT_ID" \
  --service-account "$SA" --command python --args=-m,adoption_agent.cli,poll \
  --task-timeout 14400s --max-retries 0 --cpu 2 --memory 2Gi \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,ADOPTION_AGENT_SECRETS_BUCKET=${BUCKET},ADOPTION_AGENT_APPROVAL=auto,ADOPTION_AGENT_PII_BACKEND=regex,ADOPTION_AGENT_TRACE_TO_CLOUD=false,ADOPTION_AGENT_MODEL=${MODEL},ADOPTION_AGENT_REASONING_MODEL=${MODEL}"

echo "==> Cloud Scheduler $SCHED (every 10 minutes)"
URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run"
if $GCLOUD scheduler jobs describe "$SCHED" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  $GCLOUD scheduler jobs update http "$SCHED" --location "$REGION" --project "$PROJECT_ID" \
    --schedule "*/10 * * * *" --uri "$URI" --http-method POST --oauth-service-account-email "$SA"
else
  $GCLOUD scheduler jobs create http "$SCHED" --location "$REGION" --project "$PROJECT_ID" \
    --schedule "*/10 * * * *" --uri "$URI" --http-method POST --oauth-service-account-email "$SA" \
    --description "Start the adoption research refresh job when a refresh is requested"
fi

echo "==> Done. Trigger a test poll now with:"
echo "    $GCLOUD run jobs execute $JOB --region $REGION --project $PROJECT_ID"
