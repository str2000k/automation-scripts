#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Deploy Shift Bolt Server to Google Cloud Run
# ---------------------------------------------------------------------------
# Usage:
#   ./deploy.sh              # Deploy (reads secrets from local .env)
#   ./deploy.sh --dry-run    # Print commands without executing
#
# Prerequisites:
#   - gcloud CLI authenticated: gcloud auth login
#   - Docker or gcloud builds enabled
#   - Secrets set in .env file (parent directory)
# ---------------------------------------------------------------------------

PROJECT_ID="thermal-circle-488006-b8"
REGION="asia-northeast1"
SERVICE_NAME="shift-bolt-server"
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "[DRY-RUN] Commands will be printed but not executed."
fi

# ---------------------------------------------------------------------------
# Read secrets from .env
# ---------------------------------------------------------------------------
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: .env file not found at ${ENV_FILE}"
    echo "Create it with the required environment variables."
    exit 1
fi

# Source .env (simple key=value parser)
set -a
while IFS='=' read -r key val; do
    key=$(echo "$key" | xargs)
    [[ -z "$key" || "$key" == \#* ]] && continue
    val=$(echo "$val" | xargs)
    export "$key"="$val"
done < "${ENV_FILE}"
set +a

# Validate required vars
REQUIRED_VARS=(
    SLACK_BOT_TOKEN
    SLACK_SIGNING_SECRET
    KINTONE_DOMAIN
    KINTONE_USERNAME
    KINTONE_PASSWORD
    KINTONE_SHIFT_WISH_APP_ID
)

for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: ${var} is not set in .env"
        exit 1
    fi
done

echo "=== Shift Bolt Server - Cloud Run Deployment ==="
echo "Project:  ${PROJECT_ID}"
echo "Region:   ${REGION}"
echo "Service:  ${SERVICE_NAME}"
echo "Image:    ${IMAGE_NAME}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Build container image using Cloud Build
# ---------------------------------------------------------------------------
echo "--- Step 1: Build container image ---"
BUILD_CMD="gcloud builds submit --tag ${IMAGE_NAME} --project ${PROJECT_ID} ${SCRIPT_DIR}"

if $DRY_RUN; then
    echo "[DRY-RUN] ${BUILD_CMD}"
else
    echo "Building..."
    eval "${BUILD_CMD}"
fi

# ---------------------------------------------------------------------------
# Step 2: Deploy to Cloud Run
# ---------------------------------------------------------------------------
echo ""
echo "--- Step 2: Deploy to Cloud Run ---"

# Build --set-env-vars string
ENV_VARS="SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}"
ENV_VARS+=",SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET}"
ENV_VARS+=",KINTONE_DOMAIN=${KINTONE_DOMAIN}"
ENV_VARS+=",KINTONE_USERNAME=${KINTONE_USERNAME}"
ENV_VARS+=",KINTONE_PASSWORD=${KINTONE_PASSWORD}"
ENV_VARS+=",KINTONE_SHIFT_WISH_APP_ID=${KINTONE_SHIFT_WISH_APP_ID}"

DEPLOY_CMD="gcloud run deploy ${SERVICE_NAME} \
    --image ${IMAGE_NAME} \
    --project ${PROJECT_ID} \
    --region ${REGION} \
    --platform managed \
    --allow-unauthenticated \
    --port 8080 \
    --memory 256Mi \
    --cpu 1 \
    --min-instances 0 \
    --max-instances 3 \
    --timeout 120 \
    --set-env-vars=\"${ENV_VARS}\""

if $DRY_RUN; then
    echo "[DRY-RUN] ${DEPLOY_CMD}"
else
    echo "Deploying..."
    eval "${DEPLOY_CMD}"
fi

# ---------------------------------------------------------------------------
# Step 3: Show service URL
# ---------------------------------------------------------------------------
echo ""
echo "--- Step 3: Service URL ---"
if ! $DRY_RUN; then
    SERVICE_URL=$(gcloud run services describe ${SERVICE_NAME} \
        --project ${PROJECT_ID} \
        --region ${REGION} \
        --format 'value(status.url)')
    echo ""
    echo "=== Deployment complete ==="
    echo "Service URL: ${SERVICE_URL}"
    echo ""
    echo "Next steps - Configure Slack app:"
    echo "  1. Go to https://api.slack.com/apps"
    echo "  2. Select the 'Claude MCP' app (${APP_ID:-A0AG9QRBLGH})"
    echo "  3. Interactivity & Shortcuts:"
    echo "     Request URL: ${SERVICE_URL}/slack/events"
    echo "  4. Event Subscriptions:"
    echo "     Request URL: ${SERVICE_URL}/slack/events"
    echo "     Subscribe to bot events: message.channels, message.im"
    echo "  5. Disable Socket Mode (optional, if no other services use it)"
    echo ""
    echo "  Test health: curl ${SERVICE_URL}/health"
else
    echo "[DRY-RUN] Would show service URL after deployment."
    echo ""
    echo "After deployment, configure Slack app:"
    echo "  Interactivity Request URL: <SERVICE_URL>/slack/events"
    echo "  Event Subscriptions URL:   <SERVICE_URL>/slack/events"
fi
