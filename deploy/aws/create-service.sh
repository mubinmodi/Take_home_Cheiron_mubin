#!/usr/bin/env bash
# Create the hosted service on Amazon ECS Express Mode (docs/hosted-deployment.md): one call that sets
# up Fargate tasks, an HTTPS load balancer and auto scaling from the image in ECR, then opens the
# database and cache to the service and waits until its address answers.
#
# Needs, from the earlier steps: the image clinical-trials-viz:$TAG in ECR, the roles ecsTaskExecutionRole
# and ecsInfrastructureRoleForExpressServices, the security group clinical-trials-viz-data around the
# database and cache, and the secrets clinical-trials-viz/{OPENAI_API_KEY,ANTHROPIC_API_KEY,API_KEYS,
# DATABASE_URL,REDIS_URL} in Secrets Manager.
#
#   deploy/aws/create-service.sh          # TAG=v1 by default
set -euo pipefail
source "$(dirname "$0")/lib.sh"
TAG=${TAG:-v1}
IMAGE="$REGISTRY/$NAME:$TAG"

# Express Mode runs in the "default" cluster; create it (free, empty) if this account has none yet.
if [ "$(aws ecs describe-clusters --clusters default --query 'length(clusters[?status==`ACTIVE`])' --output text)" = 0 ]; then
  aws ecs create-cluster --cluster-name default --query 'cluster.status' --output text
fi

echo "Creating $NAME from $IMAGE: 0.5 vCPU, 1 GB, ARM64, 1-2 tasks"
aws ecs create-express-gateway-service \
  --service-name "$NAME" \
  --execution-role-arn "arn:aws:iam::$ACCOUNT:role/ecsTaskExecutionRole" \
  --infrastructure-role-arn "arn:aws:iam::$ACCOUNT:role/ecsInfrastructureRoleForExpressServices" \
  --primary-container "$(container_json "$IMAGE")" \
  --cpu 512 --memory 1024 --cpu-architecture ARM64 \
  --health-check-path /health \
  --scaling-target '{"minTaskCount": 1, "maxTaskCount": 2}' \
  --query 'service.status.statusCode' --output text

open_data_firewall
wait_until_live
