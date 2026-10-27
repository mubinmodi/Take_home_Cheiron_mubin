# Shared by the deploy/aws scripts (sourced, not run). Defaults fit the "Idea Incubator" account, whose
# free plan allows regional services only in us-east-2; override with AWS_PROFILE and AWS_REGION.
export AWS_PROFILE=${AWS_PROFILE:-incubator} AWS_REGION=${AWS_REGION:-us-east-2}
NAME=clinical-trials-viz
APP_SECRETS="OPENAI_API_KEY ANTHROPIC_API_KEY API_KEYS DATABASE_URL REDIS_URL"  # read by the service's tasks
ALL_SECRETS="$APP_SECRETS db-password"
# Opt-in traces (docs/hosted-deployment.md, "Traces in X-Ray"): TRACES=xray adds an ADOT collector next to
# the app, which sends the app's OpenTelemetry spans to AWS X-Ray. Unset, nothing about traces changes.
TRACES=${TRACES:-}
TASK_ROLE="$NAME-task"  # the role the app's tasks run as; only traces need one (X-Ray write access)
TRACE_ENV="OTEL_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318"  # app -> collector, same task

step() { printf '\n==> %s\n' "$*"; }
ok() { printf '    ok: %s\n' "$*"; }
fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }
found() { "$@" >/dev/null 2>&1; }  # a read-only check: does this resource exist?
change() {  # change DESCRIPTION COMMAND...: run a command that changes AWS, or only name it when DRY_RUN is set
  local what=$1
  shift
  if [ -n "${DRY_RUN:-}" ]; then
    printf '    would: %s\n' "$what"
  else
    printf '    doing: %s\n' "$what"
    "$@" >/dev/null
  fi
}
case $TRACES in "" | xray) ;; *) fail "TRACES=$TRACES is not supported (use TRACES=xray, or leave it unset)" ;; esac

command -v aws >/dev/null || fail "the AWS CLI is not installed"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  || fail "not signed in to AWS. Run: aws login --profile $AWS_PROFILE"
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
SERVICE_ARN="arn:aws:ecs:$AWS_REGION:$ACCOUNT:service/default/$NAME"

default_vpc() { aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text; }
data_group() {  # the security group around the database and cache, in the default VPC ("None" when missing)
  aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME-data" "Name=vpc-id,Values=$(default_vpc)" \
    --query 'SecurityGroups[0].GroupId' --output text
}

service_url() {  # the service's public address
  local endpoint
  endpoint=$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
    --query 'service.activeConfigurations[0].ingressPaths[0].endpoint' --output text)
  case $endpoint in http*) echo "$endpoint" ;; *) echo "https://$endpoint" ;; esac
}

push_image() {  # push_image IMAGE: build the ARM64 image and push it to ECR
  aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
  docker build --platform linux/arm64 -t "$1" .
  docker push "$1"
}

with_settings() {  # with_settings JSON: the container JSON with the image $IMAGE and the SET_ENV settings applied
  printf '%s' "$1" | python3 -c '
import json, sys
c = json.load(sys.stdin)
c["image"] = sys.argv[1]
env = {e["name"]: e["value"] for e in c.get("environment", [])}
env.update(pair.split("=", 1) for pair in sys.argv[2].split())  # SET_ENV="NAME=value NAME=value"
c["environment"] = [{"name": k, "value": v} for k, v in env.items()]
print(json.dumps(c))' "$IMAGE" "${SET_ENV:-}${TRACES:+ $TRACE_ENV}"  # TRACES=xray: the app exports spans
}

ensure_trace_role() {  # the task role with X-Ray write access (TRACES=xray only); safe to repeat
  if found aws iam get-role --role-name "$TASK_ROLE"; then
    ok "task role $TASK_ROLE"
  else
    change "create the task role $TASK_ROLE (what the app's tasks run as)" aws iam create-role \
      --role-name "$TASK_ROLE" --assume-role-policy-document \
      "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"},\"Action\":\"sts:AssumeRole\",\"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT\"}}}]}"
    [ -n "${DRY_RUN:-}" ] || sleep 10  # a new role takes a few seconds before ECS can hand it to a task
  fi
  if found aws iam get-role-policy --role-name "$TASK_ROLE" --policy-name "write-$NAME-traces"; then
    ok "task role $TASK_ROLE can write traces to X-Ray"
  else
    # Only the two calls the collector's X-Ray exporter makes; the managed AWSXrayWriteOnlyAccess
    # would also allow reading sampling rules, which this setup does not use.
    change "let $TASK_ROLE write traces to X-Ray" aws iam put-role-policy --role-name "$TASK_ROLE" \
      --policy-name "write-$NAME-traces" --policy-document \
      '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords"],"Resource":"*"}]}'
  fi
}

container_json() {  # container_json IMAGE: the app container with its settings and secrets
  local secrets="" var arn
  for var in $APP_SECRETS; do
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

open_data_firewall() {  # let the service's tasks reach the database (5432) and the cache (6379); safe to repeat
  local data app="None" port out
  data=$(data_group)
  [ "$data" != None ] || fail "the security group $NAME-data is missing; run deploy/aws/deploy.sh"
  for _ in $(seq 1 24); do  # a new service reports its security group a little after it is created
    app=$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
      --query 'service.activeConfigurations[0].networkConfiguration.securityGroups[0]' --output text)
    [ -n "$app" ] && [ "$app" != None ] && break
    sleep 5
  done
  [ -n "$app" ] && [ "$app" != None ] || fail "the service does not report its security group yet; run the script again"
  for port in 5432 6379; do
    if ! out=$(aws ec2 authorize-security-group-ingress --group-id "$data" --protocol tcp --port "$port" \
      --source-group "$app" 2>&1); then
      case $out in *InvalidPermission.Duplicate*) ;; *) echo "$out" >&2; return 1 ;; esac  # already open is fine
    fi
  done
  echo "Database and cache open to the service's security group $app"
}

wait_until_live() {  # wait until the rollout finishes and the address answers; show the events if it fails
  local state="" url
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
  if [ "$state" != COMPLETED ]; then  # do not report old tasks as the new code being live
    echo "The rollout has not finished after 15 minutes (state: $state); the old tasks may still be serving." >&2
    aws ecs describe-services --cluster default --services "$NAME" --query 'services[0].events[:5].message' --output text >&2
    return 1
  fi
  url=$(service_url)
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
