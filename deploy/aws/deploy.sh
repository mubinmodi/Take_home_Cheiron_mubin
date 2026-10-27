#!/usr/bin/env bash
# One command for the whole AWS deployment (docs/hosted-deployment.md). It sets up whatever is missing
# (registry, roles, database, cache, secrets), builds and pushes the image, creates the service or rolls
# out the new image, waits until it answers, and runs the smoke test. Safe to run again: every step
# checks what already exists and skips it. Secret values are never printed.
#
#   deploy/aws/deploy.sh                               # set up what is missing, then ship the current code
#   DRY_RUN=1 deploy/aws/deploy.sh                     # only read: list what would be created or changed
#   TAG=v7 deploy/aws/deploy.sh                        # image tag (default: the current git commit)
#   SET_ENV="OPEN_ACCESS=true" deploy/aws/deploy.sh    # also add or change settings on the service
#   UPDATE_MODEL_KEYS=1 deploy/aws/deploy.sh           # replace the stored model keys with those in .env
#   TRACES=xray deploy/aws/deploy.sh                   # also send traces to AWS X-Ray (deploy/aws/enable-traces.sh)
#
# Needs: the AWS CLI signed in (aws login --profile incubator), Docker running, and OPENAI_API_KEY and
# ANTHROPIC_API_KEY in .env. A first deployment takes about 15 minutes, mostly waiting for the database.
set -euo pipefail
cd "$(dirname "$0")/../.."
export AWS_PROFILE=${AWS_PROFILE:-incubator} AWS_REGION=${AWS_REGION:-us-east-2}
DRY_RUN=${DRY_RUN:-}

source deploy/aws/lib.sh  # step, ok, fail, found, change; checks the AWS sign-in; NAME, ACCOUNT, ...
env_value() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"; }

# --- 0. Before changing anything ----------------------------------------------------------------------
step "0/6 Checks"
for tool in python3 curl openssl; do command -v "$tool" >/dev/null || fail "$tool is not installed"; done
ok "signed in to AWS ($AWS_PROFILE, $AWS_REGION, account $ACCOUNT)"
if [ -z "$DRY_RUN" ]; then
  command -v docker >/dev/null || fail "Docker is not installed"
  found docker info || fail "Docker is not running: start Docker Desktop"
  ok "Docker is running"
fi
for key in OPENAI_API_KEY ANTHROPIC_API_KEY; do
  [ -n "$(env_value "$key")" ] || fail "$key is missing from .env (copy .env.example and fill it in)"
done
ok "model keys found in .env"
SECRET_DIR=$(mktemp -d)  # secret values go to AWS through owner-only files, never on a command line
chmod 700 "$SECRET_DIR"
trap 'rm -rf "$SECRET_DIR"' EXIT
as_file() {  # as_file VALUE: a file:// reference to VALUE, for an AWS CLI parameter
  local file
  file=$(mktemp "$SECRET_DIR/value.XXXXXX")
  printf '%s' "$1" >"$file"
  echo "file://$file"
}
TAG=${TAG:-$(git rev-parse --short HEAD)}
[ -z "$(git status --porcelain -- src pyproject.toml uv.lock Dockerfile)" ] \
  || printf '    note: uncommitted changes in the code will be shipped; the tag %s names the last commit\n' "$TAG"

# --- 1. Image registry --------------------------------------------------------------------------------
step "1/6 Image registry"
if found aws ecr describe-repositories --repository-names "$NAME"; then
  ok "repository $NAME"
else
  change "create the ECR repository $NAME" \
    aws ecr create-repository --repository-name "$NAME" --image-scanning-configuration scanOnPush=true
fi

# --- 2. Roles -----------------------------------------------------------------------------------------
step "2/6 Roles"
if found aws iam get-role --role-name ecsTaskExecutionRole; then
  ok "ecsTaskExecutionRole"
else
  change "create ecsTaskExecutionRole (start tasks, write logs)" aws iam create-role \
    --role-name ecsTaskExecutionRole --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  change "attach the ECS task execution policy" aws iam attach-role-policy --role-name ecsTaskExecutionRole \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
fi
if found aws iam get-role-policy --role-name ecsTaskExecutionRole --policy-name "read-$NAME-secrets"; then
  ok "ecsTaskExecutionRole can read this app's secrets"
else
  change "let ecsTaskExecutionRole read this app's secrets" aws iam put-role-policy --role-name ecsTaskExecutionRole \
    --policy-name "read-$NAME-secrets" --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":\"arn:aws:secretsmanager:$AWS_REGION:*:secret:$NAME/*\"}]}"
fi
if found aws iam get-role --role-name ecsInfrastructureRoleForExpressServices; then
  ok "ecsInfrastructureRoleForExpressServices"
else
  change "create ecsInfrastructureRoleForExpressServices (load balancer, scaling)" aws iam create-role \
    --role-name ecsInfrastructureRoleForExpressServices --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Sid":"AllowAccessInfrastructureForECSExpressServices","Effect":"Allow","Principal":{"Service":"ecs.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  change "attach the Express Mode infrastructure policy" aws iam attach-role-policy \
    --role-name ecsInfrastructureRoleForExpressServices \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
fi
[ -z "$TRACES" ] || ensure_trace_role  # TRACES=xray: the task role that may write to X-Ray
# Created before the first service: the first deployment failed when they were made in the same second.
for pair in AWSServiceRoleForECS:ecs.amazonaws.com \
  AWSServiceRoleForElasticLoadBalancing:elasticloadbalancing.amazonaws.com \
  AWSServiceRoleForApplicationAutoScaling_ECSService:ecs.application-autoscaling.amazonaws.com; do
  role=${pair%%:*}
  if found aws iam get-role --role-name "$role"; then
    ok "$role"
  else
    change "create the service-linked role $role" aws iam create-service-linked-role --aws-service-name "${pair#*:}"
  fi
done

# --- 3. Database and cache ----------------------------------------------------------------------------
step "3/6 Database and cache (default VPC)"
VPC=$(default_vpc)
[ "$VPC" != None ] || fail "this region has no default VPC"
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" Name=default-for-az,Values=true \
  --query 'Subnets[].SubnetId' --output text)
DATA_SG=$(data_group)
if [ "$DATA_SG" != None ]; then
  ok "security group $NAME-data ($DATA_SG)"
else
  change "create the security group $NAME-data (no inbound rules until the service exists)" \
    aws ec2 create-security-group --group-name "$NAME-data" --vpc-id "$VPC" \
    --description "Postgres and Valkey for $NAME"
  DATA_SG=$([ -n "$DRY_RUN" ] && echo "<new group>" || data_group)
fi

secret_exists() { found aws secretsmanager describe-secret --secret-id "$NAME/$1"; }
secret_value() { aws secretsmanager get-secret-value --secret-id "$NAME/$1" --query SecretString --output text; }
if secret_exists db-password; then
  ok "database password secret"
else
  change "generate the database password (stored as $NAME/db-password)" \
    aws secretsmanager create-secret --name "$NAME/db-password" --secret-string "$(as_file "$(openssl rand -hex 24)")"
fi

rds_status=$(aws rds describe-db-instances --db-instance-identifier "$NAME" \
  --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || echo missing)
if [ "$rds_status" = deleting ]; then  # e.g. right after a teardown: wait, then create a new one
  if [ -n "$DRY_RUN" ]; then
    printf '    would: wait for the old database to finish deleting\n'
  else
    echo "    the old database is still being deleted; waiting (several minutes)..."
    aws rds wait db-instance-deleted --db-instance-identifier "$NAME"
  fi
  rds_status=missing
fi
if [ "$rds_status" != missing ]; then
  ok "RDS PostgreSQL $NAME ($rds_status)"
else
  if [ -n "$DRY_RUN" ]; then
    printf '    would: create RDS PostgreSQL %s (db.t4g.micro, 20 GB, not public)\n' "$NAME"
  else
    change "create RDS PostgreSQL $NAME (db.t4g.micro, 20 GB, not public)" aws rds create-db-instance \
      --db-instance-identifier "$NAME" --engine postgres --db-instance-class db.t4g.micro \
      --allocated-storage 20 --storage-type gp3 --db-name trials --master-username trials \
      --master-user-password "$(as_file "$(secret_value db-password)")" --vpc-security-group-ids "$DATA_SG" \
      --no-publicly-accessible --backup-retention-period 1
  fi
fi

cache_status() {
  aws elasticache describe-serverless-caches --serverless-cache-name "$NAME" \
    --query 'ServerlessCaches[0].Status' --output text 2>/dev/null || echo missing
}
if [ "$(cache_status)" = deleting ]; then
  if [ -n "$DRY_RUN" ]; then
    printf '    would: wait for the old cache to finish deleting\n'
  else
    echo "    the old cache is still being deleted; waiting..."
    for _ in $(seq 1 60); do [ "$(cache_status)" = deleting ] || break; sleep 10; done
  fi
fi
cache_now=$(cache_status)
if [ "$cache_now" != missing ] && [ "$cache_now" != deleting ]; then
  ok "ElastiCache Serverless (Valkey) $NAME ($cache_now)"
else
  # shellcheck disable=SC2086  # the subnet IDs are separate arguments
  change "create ElastiCache Serverless (Valkey) $NAME" aws elasticache create-serverless-cache \
    --serverless-cache-name "$NAME" --engine valkey --security-group-ids "$DATA_SG" --subnet-ids $SUBNETS \
    --cache-usage-limits 'DataStorage={Maximum=1,Unit=GB},ECPUPerSecond={Maximum=1000}'
fi

if [ -z "$DRY_RUN" ]; then
  echo "    waiting for the database (a new one takes about 10 minutes)..."
  aws rds wait db-instance-available --db-instance-identifier "$NAME"
  echo "    waiting for the cache..."
  for _ in $(seq 1 60); do
    status=$(aws elasticache describe-serverless-caches --serverless-cache-name "$NAME" \
      --query 'ServerlessCaches[0].Status' --output text)
    [ "$status" = available ] && break
    sleep 10
  done
  [ "$status" = available ] || fail "the cache is not available yet (status: $status); run the script again"
  ok "database and cache are available"
fi

# --- 4. Secrets ---------------------------------------------------------------------------------------
step "4/6 Secrets (values never printed)"
put_secret() {  # put_secret NAME VALUE: create it if missing; replace it only when REPLACE=1
  if secret_exists "$1"; then
    if [ -n "${REPLACE:-}" ]; then
      change "replace the secret $NAME/$1" aws secretsmanager put-secret-value --secret-id "$NAME/$1" \
        --secret-string "$(as_file "$2")"
    else
      ok "secret $NAME/$1"
    fi
  else
    change "create the secret $NAME/$1" aws secretsmanager create-secret --name "$NAME/$1" --secret-string "$(as_file "$2")"
  fi
}
REPLACE=${UPDATE_MODEL_KEYS:-} put_secret OPENAI_API_KEY "$(env_value OPENAI_API_KEY)"
REPLACE=${UPDATE_MODEL_KEYS:-} put_secret ANTHROPIC_API_KEY "$(env_value ANTHROPIC_API_KEY)"
put_secret API_KEYS "admin:$(openssl rand -hex 20)"  # kept as it is when it exists
if [ -z "$DRY_RUN" ]; then
  db_host=$(aws rds describe-db-instances --db-instance-identifier "$NAME" \
    --query 'DBInstances[0].Endpoint.Address' --output text)
  cache_host=$(aws elasticache describe-serverless-caches --serverless-cache-name "$NAME" \
    --query 'ServerlessCaches[0].Endpoint.Address' --output text)
  put_secret DATABASE_URL "postgresql+asyncpg://trials:$(secret_value db-password)@$db_host:5432/trials?ssl=require"
  put_secret REDIS_URL "rediss://$cache_host:6379/0"
else
  secret_exists DATABASE_URL && ok "secret $NAME/DATABASE_URL" || printf '    would: create the secret %s/DATABASE_URL\n' "$NAME"
  secret_exists REDIS_URL && ok "secret $NAME/REDIS_URL" || printf '    would: create the secret %s/REDIS_URL\n' "$NAME"
fi

# --- 5. Image and service -----------------------------------------------------------------------------
step "5/6 Image $NAME:$TAG and the service"
service_status() {
  aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
    --query 'service.status.statusCode' --output text 2>/dev/null || echo MISSING
}
STATUS=$(service_status)
if [ -n "$DRY_RUN" ]; then
  printf '    would: build and push %s:%s\n' "$NAME" "$TAG"
  if [ "$STATUS" = ACTIVE ]; then
    printf '    would: roll out %s:%s to the running service (settings kept%s)\n' "$NAME" "$TAG" \
      "${SET_ENV:+, plus $SET_ENV}"
  else
    printf '    would: create the service %s (status now: %s)\n' "$NAME" "$STATUS"
  fi
  [ -z "$TRACES" ] || printf '    would: run as %s, add the trace collector next to the app (deploy/aws/enable-traces.sh)\n' \
    "$TASK_ROLE"
  step "Dry run finished: nothing was changed. Run without DRY_RUN=1 to do the steps marked 'would'."
  exit 0
fi
if [ "$STATUS" = ACTIVE ]; then
  TAG=$TAG deploy/aws/update-service.sh  # builds, pushes, swaps the image, keeps the settings, waits
else
  push_image "$REGISTRY/$NAME:$TAG"
  TAG=$TAG deploy/aws/create-service.sh  # creates the service, opens the data firewall, waits
fi

# --- 6. Smoke test ------------------------------------------------------------------------------------
step "6/6 Smoke test"
url=$(service_url)
API_KEY=$(secret_value API_KEYS | cut -d, -f1 | cut -d: -f2-) deploy/smoke-test.sh "$url"
step "Deployed: $url"
