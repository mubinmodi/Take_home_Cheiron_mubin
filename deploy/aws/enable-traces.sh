#!/usr/bin/env bash
# Turn traces on for the running service (docs/hosted-deployment.md, "Traces in X-Ray"): the app's
# OpenTelemetry spans go over OTLP to an ADOT collector in the same task, which sends them to AWS X-Ray.
#
# Express Mode has no sidecar setting of its own, but it keeps a sidecar added to the task definition it
# manages (https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-advanced-customization.html).
# So this script copies the service's current task definition, adds the collector container, runs the
# tasks as the task role that may write to X-Ray, tells the app where the collector is, registers that as
# a new revision and rolls it out (a rolling update, rolled back automatically if it fails).
#
# Safe to run again: when the running task definition already has all of that, it changes nothing.
# deploy.sh, create-service.sh and update-service.sh run it when TRACES=xray.
#
#   DRY_RUN=1 deploy/aws/enable-traces.sh    # only read: say what would change
#   deploy/aws/enable-traces.sh              # create the task role if missing, add the collector, roll out
set -euo pipefail
cd "$(dirname "$0")/../.."
DRY_RUN=${DRY_RUN:-}
TRACES=xray  # this script is the opt-in
source deploy/aws/lib.sh  # step, ok, fail, found, change; checks the AWS sign-in; NAME, ACCOUNT, ...
COLLECTOR_IMAGE=public.ecr.aws/aws-observability/aws-otel-collector:v0.50.0  # pinned; ARM64 and x86

step "Traces: task role"
ensure_trace_role

step "Traces: the collector next to the app"
CURRENT_TD=$(aws ecs describe-services --cluster default --services "$NAME" \
  --query 'services[0].taskDefinition' --output text 2>/dev/null) || CURRENT_TD=None
[ -n "$CURRENT_TD" ] && [ "$CURRENT_TD" != None ] || fail "the service $NAME is not running; run deploy/aws/deploy.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
aws ecs describe-task-definition --task-definition "$CURRENT_TD" --include TAGS --output json >"$WORK/current.json"

# Writes the new revision to new.json, or nothing when the current one already sends traces.
python3 - "$WORK/current.json" "$WORK/new.json" "$COLLECTOR_IMAGE" deploy/aws/otel-collector.yaml \
  "arn:aws:iam::$ACCOUNT:role/$TASK_ROLE" "$AWS_REGION" "$TRACE_ENV" <<'PY'
import copy, json, sys

current_path, new_path, image, config_path, role_arn, region, trace_env = sys.argv[1:]
described = json.load(open(current_path))
td = described["taskDefinition"]
config = open(config_path).read()
COLLECTOR, COLLECTOR_MEMORY = "otel-collector", 64  # MiB reserved for the collector inside the task

containers = td["containerDefinitions"]
main = next((c for c in containers if c["name"] == "Main"), containers[0])
others = [c for c in containers if c is not main and c["name"] != COLLECTOR]

collector = {
    "name": COLLECTOR,
    "image": image,
    "essential": False,  # if the collector stops, the app keeps serving; only traces stop
    "memoryReservation": COLLECTOR_MEMORY,
    "environment": [
        {"name": "AOT_CONFIG_CONTENT", "value": config},
        {"name": "AWS_REGION", "value": region},
    ],
}
log = main.get("logConfiguration")
if log and log.get("logDriver") == "awslogs":  # same log group as the app, its own stream prefix
    log = copy.deepcopy(log)
    log.setdefault("options", {})["awslogs-stream-prefix"] = COLLECTOR
    collector["logConfiguration"] = log

new_main = copy.deepcopy(main)
env = {e["name"]: e["value"] for e in new_main.get("environment", [])}
env.update(pair.split("=", 1) for pair in trace_env.split())
new_main["environment"] = [{"name": k, "value": v} for k, v in env.items()]
task_memory = int(td.get("memory", 0) or 0)
for key in ("memoryReservation", "memory"):  # leave room for the collector inside the task's memory
    if task_memory and new_main.get(key) and new_main[key] + COLLECTOR_MEMORY > task_memory:
        new_main[key] = task_memory - COLLECTOR_MEMORY

current_collector = next((c for c in containers if c["name"] == COLLECTOR), None)
done = (
    td.get("taskRoleArn") == role_arn
    and new_main == main
    and current_collector is not None
    and current_collector.get("image") == image
    and current_collector.get("environment") == collector["environment"]
)
if done:
    sys.exit(0)

KEEP = ("family", "executionRoleArn", "networkMode", "volumes", "placementConstraints",
        "requiresCompatibilities", "cpu", "memory", "pidMode", "ipcMode", "proxyConfiguration",
        "ephemeralStorage", "runtimePlatform", "enableFaultInjection")
new = {k: td[k] for k in KEEP if k in td}
new["taskRoleArn"] = role_arn
new["containerDefinitions"] = [new_main, *others, collector]
tags = [t for t in described.get("tags", []) if not t["key"].startswith("aws:")]  # aws: tags are AWS's own
if tags:
    new["tags"] = tags
json.dump(new, open(new_path, "w"))
PY

if [ ! -s "$WORK/new.json" ]; then
  ok "the running task definition (${CURRENT_TD##*/}) already sends traces to X-Ray"
  exit 0
fi
if [ -n "$DRY_RUN" ]; then
  printf '    would: register a new revision of %s with the collector %s, the task role %s\n' \
    "${CURRENT_TD##*/}" "${COLLECTOR_IMAGE##*/}" "$TASK_ROLE"
  printf '    would: set %s on the app, then roll the new revision out\n' "$TRACE_ENV"
  step "Dry run finished: nothing was changed. Run without DRY_RUN=1 to turn traces on."
  exit 0
fi
NEW_TD=$(aws ecs register-task-definition --cli-input-json "file://$WORK/new.json" \
  --query 'taskDefinition.taskDefinitionArn' --output text)
ok "registered ${NEW_TD##*/} (app + collector, runs as $TASK_ROLE)"
change "roll out ${NEW_TD##*/}" aws ecs update-service --cluster default --service "$NAME" \
  --task-definition "$NEW_TD" --force-new-deployment
wait_until_live
step "Traces on: CloudWatch console > X-Ray traces > Traces ($AWS_REGION), service clinical-trials-viz"
