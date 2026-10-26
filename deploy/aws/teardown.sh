#!/usr/bin/env bash
# Delete the whole AWS deployment (docs/hosted-deployment.md, "Costs and teardown"): the service and its
# load balancer, the cache, the database (no final snapshot) and its run history, the images, the secrets,
# the security group, the task definitions, the log groups, the two roles and the empty "default" ECS
# cluster (the account is used only for this project). This cannot be undone; deploy/aws/deploy.sh can
# set everything up again later, under a new address. Safe to run again: what is already gone is skipped.
# AWS's service-linked roles are kept: they are shared by the account, free, and AWS manages them.
#
#   DRY_RUN=1 deploy/aws/teardown.sh     # only read: list what would be deleted
#   deploy/aws/teardown.sh               # asks you to type the service name, then deletes
set -euo pipefail
cd "$(dirname "$0")/../.."
export AWS_PROFILE=${AWS_PROFILE:-incubator} AWS_REGION=${AWS_REGION:-us-east-2}
DRY_RUN=${DRY_RUN:-}

source deploy/aws/lib.sh  # step, ok, fail, found; checks the AWS sign-in; NAME, ACCOUNT, SERVICE_ARN, ...
remove() {  # remove DESCRIPTION COMMAND...: delete something, or only name it in a dry run
  local what=$1
  shift
  if [ -n "$DRY_RUN" ]; then
    printf '    would delete: %s\n' "$what"
  else
    printf '    deleting: %s\n' "$what"
    "$@" >/dev/null
  fi
}
wait_gone() {  # wait_gone DESCRIPTION COMMAND...: poll until the read command fails (the thing is gone)
  local what=$1
  shift
  [ -n "$DRY_RUN" ] && return 0
  printf '    waiting until %s is gone' "$what"
  for _ in $(seq 1 90); do
    if ! found "$@"; then
      printf ' - gone\n'
      return 0
    fi
    printf '.'
    sleep 10
  done
  printf '\n'
  fail "$what is still there after 15 minutes; run the script again later"
}

step "Checks"
ok "signed in to AWS ($AWS_PROFILE, $AWS_REGION, account $ACCOUNT)"
if [ -z "$DRY_RUN" ]; then
  printf '\nThis permanently deletes %s from account %s in %s, including its database.\n' "$NAME" "$ACCOUNT" "$AWS_REGION"
  read -rp "Type the service name ($NAME) to continue: " answer
  [ "$answer" = "$NAME" ] || fail "nothing was deleted"
fi

step "1/7 The service, its load balancer and its tasks"
service_there() {
  local status
  status=$(aws ecs describe-express-gateway-service --service-arn "$SERVICE_ARN" \
    --query 'service.status.statusCode' --output text 2>/dev/null) || return 1
  [ "$status" != INACTIVE ] && [ "$status" != None ]
}
DATA_SG=$(data_group)
if service_there; then
  # The database firewall names the service's security group; drop those rules first, so the
  # service's group can be deleted along with it.
  if [ "$DATA_SG" != None ]; then
    rules=$(aws ec2 describe-security-group-rules --filters "Name=group-id,Values=$DATA_SG" \
      --query 'SecurityGroupRules[?!IsEgress].SecurityGroupRuleId' --output text)
    if [ -n "$rules" ] && [ "$rules" != None ]; then
      # shellcheck disable=SC2086  # the rule IDs are separate arguments
      remove "the database and cache firewall rules that admit the service" \
        aws ec2 revoke-security-group-ingress --group-id "$DATA_SG" --security-group-rule-ids $rules
    fi
  fi
  remove "the ECS Express Mode service $NAME (and its load balancer)" \
    aws ecs delete-express-gateway-service --service-arn "$SERVICE_ARN"
  wait_gone "the service" service_there
else
  ok "no service"
fi

step "2/7 The cache"
if found aws elasticache describe-serverless-caches --serverless-cache-name "$NAME"; then
  remove "ElastiCache Serverless $NAME" aws elasticache delete-serverless-cache --serverless-cache-name "$NAME"
else
  ok "no cache"
fi

step "3/7 The database (no final snapshot)"
if found aws rds describe-db-instances --db-instance-identifier "$NAME"; then
  remove "RDS PostgreSQL $NAME and its automated backups" aws rds delete-db-instance \
    --db-instance-identifier "$NAME" --skip-final-snapshot --delete-automated-backups
else
  ok "no database"
fi
wait_gone "the cache" aws elasticache describe-serverless-caches --serverless-cache-name "$NAME"
wait_gone "the database (this takes several minutes)" aws rds describe-db-instances --db-instance-identifier "$NAME"

step "4/7 The security group"
if [ "$DATA_SG" != None ]; then
  remove "the security group $NAME-data ($DATA_SG)" aws ec2 delete-security-group --group-id "$DATA_SG"
else
  ok "no security group"
fi

step "5/7 Images, secrets and logs"
if found aws ecr describe-repositories --repository-names "$NAME"; then
  remove "the ECR repository $NAME and all its images" aws ecr delete-repository --repository-name "$NAME" --force
else
  ok "no image repository"
fi
for secret in $ALL_SECRETS; do
  if found aws secretsmanager describe-secret --secret-id "$NAME/$secret"; then
    remove "the secret $NAME/$secret" aws secretsmanager delete-secret --secret-id "$NAME/$secret" \
      --force-delete-without-recovery
  fi
done
ok "secrets done"
for group in $(aws logs describe-log-groups --query "logGroups[?contains(logGroupName, '$NAME')].logGroupName" \
  --output text); do
  [ "$group" = None ] || remove "the log group $group" aws logs delete-log-group --log-group-name "$group"
done
ok "log groups done"
# Express Mode registers a task definition revision per rollout; deregister them, then delete them.
for arn in $(aws ecs list-task-definitions --status ACTIVE \
  --query "taskDefinitionArns[?contains(@, '$NAME')]" --output text); do
  [ "$arn" = None ] || remove "the task definition ${arn##*/}" aws ecs deregister-task-definition --task-definition "$arn"
done
if [ -z "$DRY_RUN" ]; then
  inactive=$(aws ecs list-task-definitions --status INACTIVE \
    --query "taskDefinitionArns[?contains(@, '$NAME')]" --output text)
  # shellcheck disable=SC2086  # delete-task-definitions takes at most 10 at a time
  [ -z "$inactive" ] || [ "$inactive" = None ] || printf '%s\n' $inactive | xargs -n 10 \
    aws ecs delete-task-definitions --query 'failures[].reason' --output text --task-definitions >/dev/null
fi
ok "task definitions done"

step "6/7 Roles"
delete_role() {  # delete_role NAME: detach its managed policies, drop its inline ones, delete it
  local role=$1 policy
  if ! found aws iam get-role --role-name "$role"; then
    ok "no role $role"
    return 0
  fi
  for policy in $(aws iam list-attached-role-policies --role-name "$role" \
    --query 'AttachedPolicies[].PolicyArn' --output text); do
    remove "the policy ${policy##*/} from $role" aws iam detach-role-policy --role-name "$role" --policy-arn "$policy"
  done
  for policy in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text); do
    remove "the inline policy $policy of $role" aws iam delete-role-policy --role-name "$role" --policy-name "$policy"
  done
  remove "the role $role" aws iam delete-role --role-name "$role"
}
delete_role ecsTaskExecutionRole
delete_role ecsInfrastructureRoleForExpressServices
echo "    kept: AWS's service-linked roles for ECS, load balancing and auto scaling (shared, free)"

step "7/7 The empty ECS cluster"
if [ "$(aws ecs describe-clusters --clusters default --query 'clusters[?status==`ACTIVE`].activeServicesCount | [0]' \
  --output text 2>/dev/null)" = 0 ]; then
  remove "the empty ECS cluster 'default'" aws ecs delete-cluster --cluster default
else
  ok "no empty cluster to delete"
fi

if [ -n "$DRY_RUN" ]; then
  step "Dry run finished: nothing was deleted. Run without DRY_RUN=1 to delete what is listed."
else
  step "Done: $NAME is gone from $AWS_REGION. deploy/aws/deploy.sh sets it up again (under a new address)."
fi
