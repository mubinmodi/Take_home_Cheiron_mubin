# Shared by the deploy/aws scripts (sourced, not run). Defaults fit the "Idea Incubator" account, whose
# free plan allows regional services only in us-east-2; override with AWS_PROFILE and AWS_REGION.
export AWS_PROFILE=${AWS_PROFILE:-incubator} AWS_REGION=${AWS_REGION:-us-east-2}
NAME=clinical-trials-viz
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
SERVICE_ARN="arn:aws:ecs:$AWS_REGION:$ACCOUNT:service/default/$NAME"

container_json() {  # container_json IMAGE: the app container with its settings and secrets
  local secrets="" var arn
  for var in OPENAI_API_KEY ANTHROPIC_API_KEY API_KEYS DATABASE_URL REDIS_URL; do
    # ECS reads each secret into an environment variable of the same name when a task starts.
    arn=$(aws secretsmanager describe-secret --secret-id "$NAME/$var" --query ARN --output text)
    secrets+="{\"name\": \"$var\", \"valueFrom\": \"$arn\"},"
  done
  cat <<EOF
{
  "image": "$1",
  "containerPort": 8080,
  "environment": [
    {"name": "DEPLOYMENT", "value": "hosted"},
    {"name": "RUN_DEADLINE_SECONDS", "value": "30"},
    {"name": "USER_QUERIES_PER_HOUR", "value": "30"},
    {"name": "LOG_LEVEL", "value": "INFO"}
  ],
  "secrets": [${secrets%,}]
}
EOF
}

open_data_firewall() {  # let the service's tasks reach the database (5432) and the cache (6379)
  local data app port out
  data=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME-data" \
    --query 'SecurityGroups[0].GroupId' --output text)
  app=$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
    --query 'service.activeConfigurations[0].networkConfiguration.securityGroups[0]' --output text)
  for port in 5432 6379; do
    if ! out=$(aws ec2 authorize-security-group-ingress --group-id "$data" --protocol tcp --port "$port" \
      --source-group "$app" 2>&1); then
      case $out in *InvalidPermission.Duplicate*) ;; *) echo "$out" >&2; return 1 ;; esac  # already open is fine
    fi
  done
  echo "Database and cache open to the service's security group $app"
}

wait_until_live() {  # wait until the rollout finishes and the address answers; show the events if it fails
  local state endpoint url
  echo "Waiting for the rollout: new tasks must pass their health checks (usually 3-6 minutes)..."
  for _ in $(seq 1 60); do
    state=$(aws ecs describe-services --cluster default --services "$NAME" \
      --query 'services[0].deployments[0].rolloutState' --output text)
    [ "$state" = COMPLETED ] && break
    if [ "$state" = FAILED ]; then
      aws ecs describe-services --cluster default --services "$NAME" --query 'services[0].events[:5].message' --output text >&2
      return 1
    fi
    sleep 15
  done
  endpoint=$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
    --query 'service.activeConfigurations[0].ingressPaths[0].endpoint' --output text)
  case $endpoint in http*) url=$endpoint ;; *) url="https://$endpoint" ;; esac
  for _ in $(seq 1 24); do  # the load balancer can take a few seconds more to send traffic to new tasks
    if [ "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$url/health")" = 200 ]; then
      echo "Live: $url"
      return 0
    fi
    sleep 5
  done
  echo "The rollout finished, but $url/health does not answer yet" >&2
  return 1
}
