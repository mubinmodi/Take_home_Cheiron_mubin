#!/usr/bin/env bash
# Roll out new code: build the image, push it to ECR and switch the running service to it. Express Mode
# replaces tasks one by one, so the service stays up; a new image that fails its health checks is rolled
# back automatically. Only the image changes: settings, secrets and logging stay as they are.
#
#   TAG=v2 deploy/aws/update-service.sh
#   TAG=v6 SET_ENV="OPEN_ACCESS=true" deploy/aws/update-service.sh   # also add or change settings
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/aws/lib.sh
TAG=${TAG:?set TAG to a new image tag, e.g. TAG=v2}
IMAGE="$REGISTRY/$NAME:$TAG"

aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker build --platform linux/arm64 -t "$IMAGE" .
docker push "$IMAGE"

CURRENT=$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
  --query 'service.activeConfigurations[0].primaryContainer' --output json)
UPDATED=$(printf '%s' "$CURRENT" | python3 -c '
import json, sys
c = json.load(sys.stdin)
c["image"] = sys.argv[1]
env = {e["name"]: e["value"] for e in c.get("environment", [])}
env.update(pair.split("=", 1) for pair in sys.argv[2].split())  # SET_ENV="NAME=value NAME=value"
c["environment"] = [{"name": k, "value": v} for k, v in env.items()]
print(json.dumps(c))' "$IMAGE" "${SET_ENV:-}")
echo "Switching $NAME to $IMAGE"
aws ecs update-express-gateway-service --service-arn "$SERVICE_ARN" --primary-container "$UPDATED" \
  --query 'service.status.statusCode' --output text
wait_until_live
