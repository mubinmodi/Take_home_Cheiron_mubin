#!/usr/bin/env bash
# Roll out new code: build the image, push it to ECR and switch the running service to it. Express Mode
# replaces tasks one by one, so the service stays up; a new image that fails its health checks is rolled
# back automatically. Only the image (and any SET_ENV settings) change: secrets and logging stay as they are.
#
#   TAG=v2 deploy/aws/update-service.sh
#   TAG=v6 SET_ENV="OPEN_ACCESS=true" deploy/aws/update-service.sh   # also add or change settings
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/aws/lib.sh
TAG=${TAG:?set TAG to a new image tag, e.g. TAG=v2}
IMAGE="$REGISTRY/$NAME:$TAG"

push_image "$IMAGE"

CURRENT=$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
  --query 'service.activeConfigurations[0].primaryContainer' --output json)
UPDATED=$(with_settings "$CURRENT")
echo "Switching $NAME to $IMAGE"
aws ecs update-express-gateway-service --service-arn "$SERVICE_ARN" --primary-container "$UPDATED" \
  --query 'service.status.statusCode' --output text
open_data_firewall  # already open is fine; this repairs a first deployment that stopped before it
wait_until_live
