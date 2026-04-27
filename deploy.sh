#!/usr/bin/env bash
# PAS-SCADA pipeline — single-command deploy script.
#
# Applies all manifests in dependency order, waits for each piece to be
# ready, and returns non-zero if anything fails. Idempotent: safe to
# re-run after fixes.
#
# Usage:
#   ./deploy.sh                  # deploy everything (default)
#   ./deploy.sh monitor          # deploy just the monitor
#   ./deploy.sh bootstrap        # only run the bootstrap Jobs
#   ./deploy.sh connect          # connect worker + connectors
#   ./deploy.sh demo             # customer-demo UI
#
# Environment overrides:
#   NAMESPACE=pinkline           # target namespace
#   IMAGE_REGISTRY=pinkline      # registry prefix used in deployments
#   IMAGE_TAG=latest             # image tag used in deployments
#   SKIP_IMAGE_CHECK=1           # skip the "image exists in registry" check
#
# Prerequisites:
#   - kubectl configured for the target cluster
#   - 3 images pushed to ${IMAGE_REGISTRY}/{pas-scada-monitor,
#     pas-scada-connect, pas-scada-demo}:${IMAGE_TAG}
#   - Real credentials in monitor-secret, connect-secret
#   - The existing pas-scada-bridge (tms/) keeps doing the transforms;
#     flip BRIDGE_INPUT_FROM_KAFKA=true on it once Connect's source
#     connector is producing to the input topic.

set -euo pipefail

NAMESPACE="${NAMESPACE:-pinkline}"
TARGET="${1:-all}"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# ── helpers ───────────────────────────────────────────────────────

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; }

require() {
  command -v "$1" >/dev/null 2>&1 || { err "missing command: $1"; exit 1; }
}

apply() {
  log "kubectl apply -f $1"
  kubectl apply -f "$1"
}

wait_job() {
  local job="$1" timeout="${2:-180s}"
  log "Waiting for Job/$job (timeout $timeout) ..."
  if kubectl -n "$NAMESPACE" wait --for=condition=complete \
        --timeout="$timeout" "job/$job"; then
    ok "Job/$job completed"
  else
    err "Job/$job did not complete in $timeout"
    kubectl -n "$NAMESPACE" logs "job/$job" --tail=80 || true
    exit 1
  fi
}

wait_deploy() {
  local d="$1" timeout="${2:-180s}"
  log "Waiting for Deployment/$d to become available (timeout $timeout) ..."
  if kubectl -n "$NAMESPACE" wait --for=condition=available \
        --timeout="$timeout" "deploy/$d"; then
    ok "Deployment/$d available"
  else
    err "Deployment/$d did not become available in $timeout"
    kubectl -n "$NAMESPACE" describe "deploy/$d" | tail -40 || true
    exit 1
  fi
}

# ── pre-flight ────────────────────────────────────────────────────

preflight() {
  log "Pre-flight checks"
  require kubectl

  # Cluster reachable?
  if ! kubectl get ns >/dev/null 2>&1; then
    err "kubectl cannot reach the cluster"; exit 1
  fi
  ok "cluster reachable"

  # Namespace exists?
  if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
    err "namespace $NAMESPACE missing — apply tms/k8s/00-namespace.yaml first"
    exit 1
  fi
  ok "namespace $NAMESPACE present"

  # Kafka in-cluster check (warn only — external Kafka also works)
  if kubectl -n "$NAMESPACE" get deploy/kafka >/dev/null 2>&1; then
    ok "Kafka deploy found in $NAMESPACE"
  else
    warn "no in-cluster kafka deploy found — assuming external Kafka"
  fi
}

# ── steps ────────────────────────────────────────────────────────

deploy_monitor() {
  log "── monitor ────────────────────────────────────────"
  apply "$SCRIPT_DIR/monitor/k8s/10-configmap.yaml"
  apply "$SCRIPT_DIR/monitor/k8s/20-secret.yaml"
  apply "$SCRIPT_DIR/monitor/k8s/30-pvc.yaml"
  apply "$SCRIPT_DIR/monitor/k8s/40-deployment.yaml"
  wait_deploy pas-scada-monitor 120s
}

deploy_bootstrap() {
  log "── bootstrap topics + queue ──────────────────────"
  # Connect secret must exist before the RMQ Job (it pulls creds from it).
  apply "$SCRIPT_DIR/connect/k8s/10-secret.yaml"
  # Re-applying a Job is forbidden if it exists; delete-then-apply.
  kubectl -n "$NAMESPACE" delete job bootstrap-kafka-topics --ignore-not-found
  kubectl -n "$NAMESPACE" delete job bootstrap-rabbitmq-queue --ignore-not-found
  apply "$SCRIPT_DIR/bootstrap/k8s/10-kafka-topics-job.yaml"
  wait_job bootstrap-kafka-topics 180s
  apply "$SCRIPT_DIR/bootstrap/k8s/20-rabbitmq-queue-job.yaml"
  wait_job bootstrap-rabbitmq-queue 120s
}

deploy_connect() {
  log "── kafka connect worker + connectors ─────────────"
  apply "$SCRIPT_DIR/connect/k8s/10-secret.yaml"
  apply "$SCRIPT_DIR/connect/k8s/20-configmap.yaml"
  apply "$SCRIPT_DIR/connect/k8s/30-deployment.yaml"
  wait_deploy kafka-connect 240s
  kubectl -n "$NAMESPACE" delete job register-connectors --ignore-not-found
  apply "$SCRIPT_DIR/connect/k8s/40-job-register.yaml"
  wait_job register-connectors 180s
}

deploy_demo() {
  log "── demo (customer-facing UI) ─────────────────────"
  apply "$SCRIPT_DIR/demo/k8s/10-configmap.yaml"
  apply "$SCRIPT_DIR/demo/k8s/20-deployment.yaml"
  wait_deploy pas-scada-demo 120s
}

post_check() {
  log "── post-deploy summary ───────────────────────────"
  kubectl -n "$NAMESPACE" get deploy,job,svc -l 'app in (pas-scada-monitor,kafka-connect,pas-scada-bridge,pas-scada-demo)' \
    || kubectl -n "$NAMESPACE" get deploy,job,svc
  echo
  log "Connector statuses:"
  kubectl -n "$NAMESPACE" run -i --rm probe --image=curlimages/curl:8.10.1 --restart=Never -- \
      curl -fsS "http://kafka-connect.${NAMESPACE}.svc.cluster.local:8083/connectors?expand=status" 2>/dev/null \
    | tr ',' '\n' | grep -E '"name"|"state"' | head -40 || true
  echo
  ok "Deploy complete."
  echo
  echo "Verify the full pipeline by running the e2e test in connect/README.md:"
  echo "  publish XML to TMS Artemis, then watch tms.raw → tms.scada.encrypted → RabbitMQ → SCADA."
}

# ── main ─────────────────────────────────────────────────────────

main() {
  preflight
  case "$TARGET" in
    monitor)   deploy_monitor ;;
    bootstrap) deploy_bootstrap ;;
    connect)   deploy_connect ;;
    demo)      deploy_demo ;;
    all)
      deploy_monitor
      deploy_bootstrap
      deploy_connect
      deploy_demo
      post_check
      ;;
    *)
      err "unknown target: $TARGET (expected: all|monitor|bootstrap|connect|demo)"
      exit 1
      ;;
  esac
}

main "$@"
