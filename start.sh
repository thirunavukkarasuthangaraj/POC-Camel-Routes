#!/usr/bin/env bash
# start.sh — bring up the client-requested deployment end-to-end.
#
#   - Artemis: Docker (from D:/pinkline/code/messaging-infra)
#   - Everything else (Zookeeper, Kafka, Kafdrop, bridge, RabbitMQ,
#     SCADA API): Docker Desktop Kubernetes (kubectl context docker-desktop)
#
# Docker Desktop k8s shares the host Docker daemon, so locally-built
# images are available to pods immediately — no `minikube image load`
# step required.
#
# Idempotent: re-running after a crash or partial failure converges to the
# desired state. Safe to invoke repeatedly.
#
# Prerequisites: docker, kubectl on PATH; Docker Desktop with Kubernetes
# enabled (Settings → Kubernetes → Enable Kubernetes).

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
MESSAGING_INFRA="${MESSAGING_INFRA:-/d/pinkline/code/messaging-infra}"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 0a. Kill rogue host containers that bind ports we'll port-forward ───
# A previous standalone `docker run` of scada-api / monitor / etc. can keep
# binding 0.0.0.0:8091 etc. after we move to k8s. Chrome then hits the old
# container instead of `kubectl port-forward` (which binds 127.0.0.1 only).
# Symptom: dashboards show old UI no matter how many times you rebuild.
log "Removing any host-side scada-api/monitor/demo containers"
for name in pas-scada-api pas-scada-monitor pas-scada-demo external-scada-scada-api; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    docker rm -f "$name" >/dev/null 2>&1 || true
    ok "removed rogue container: $name"
  fi
done

# ── 0b. Delete orphan Kafka Connect resources (from before Connect was removed) ──
# We removed connect/ from the repo, but a previously-applied kafka-connect
# Deployment/Service/ConfigMap/Secret can still linger in the cluster and
# crashloop indefinitely. Clean them defensively each run.
log "Cleaning orphan Kafka Connect resources"
kubectl -n pinkline delete deploy kafka-connect    --ignore-not-found 2>/dev/null || true
kubectl -n pinkline delete svc    kafka-connect    --ignore-not-found 2>/dev/null || true
kubectl -n pinkline delete cm     connectors       --ignore-not-found 2>/dev/null || true
kubectl -n pinkline delete cm     connect-config   --ignore-not-found 2>/dev/null || true
kubectl -n pinkline delete secret connect-secret   --ignore-not-found 2>/dev/null || true
kubectl -n pinkline delete job    register-connectors --ignore-not-found 2>/dev/null || true
ok "orphan Connect resources cleaned (if any)"

# ── 1. Docker Desktop Kubernetes ────────────────────────────────────────
log "Ensuring docker-desktop kubectl context"
if ! kubectl config get-contexts docker-desktop >/dev/null 2>&1; then
  die "docker-desktop kubectl context not found. Enable Kubernetes in Docker Desktop → Settings → Kubernetes, then retry."
fi
kubectl config use-context docker-desktop >/dev/null
if ! kubectl get nodes 2>/dev/null | grep -q ' Ready '; then
  die "docker-desktop node is not Ready. Open Docker Desktop and wait for the kubectl light to turn green."
fi
ok "docker-desktop k8s ready"

# ── 2. Artemis (Docker on host) ─────────────────────────────────────────
log "Starting Artemis from $MESSAGING_INFRA"
[ -f "$MESSAGING_INFRA/docker-compose.yml" ] \
  || die "$MESSAGING_INFRA/docker-compose.yml not found — set MESSAGING_INFRA env var if path differs"
docker compose -f "$MESSAGING_INFRA/docker-compose.yml" up -d
ok "Artemis up (port 61616 / console 8161)"

# ── 3. Build Bridge image from current source ───────────────────────────
# Rebuild whenever Java sources change. Maven layer cache keeps it cheap
# when nothing changed; full build is ~3 min on cold cache.
log "Building Bridge image"
docker build -q -t pinkline/pas-scada-bridge:latest "$SCRIPT_DIR/tms/" >/dev/null
ok "bridge image built"

# ── 4. Build SCADA API image from current source ────────────────────────
# Always rebuild so app.py edits ship into the running pod. Layers are
# cached so this is cheap when nothing changed.
log "Building SCADA API image"
docker build -q \
  -t ghcr.io/thirunavukkarasuthangaraj/pas-scada-api:latest \
  -t external-scada-scada-api:latest \
  "$SCRIPT_DIR/external-scada/scada-api/" >/dev/null
ok "scada-api image built"

# ── 4b. Build Monitor + Demo images ─────────────────────────────────────
log "Building Monitor image"
docker build -q -t pinkline/pas-scada-monitor:latest "$SCRIPT_DIR/monitor/" >/dev/null
ok "monitor image built"

log "Building Demo image"
docker build -q -t pinkline/pas-scada-demo:1.0.0 "$SCRIPT_DIR/demo/" >/dev/null
ok "demo image built"

# ── 5. (No image-load step) ─────────────────────────────────────────────
# Docker Desktop k8s reads images straight from the host Docker daemon —
# locally-built `docker build` images are available to pods immediately,
# no minikube-style image-load required.
log "Skipping image load (Docker Desktop k8s shares host docker images)"
ok "host docker daemon = cluster image source"

# ── 6. Apply tms/k8s manifests ──────────────────────────────────────────
log "Applying tms/k8s manifests"
kubectl apply -f "$SCRIPT_DIR/tms/k8s/00-namespace.yaml"
kubectl apply -f "$SCRIPT_DIR/tms/k8s/20-zookeeper.yaml"
kubectl apply -f "$SCRIPT_DIR/tms/k8s/30-kafka.yaml"
kubectl apply -f "$SCRIPT_DIR/tms/k8s/40-kafdrop.yaml"
kubectl apply -f "$SCRIPT_DIR/tms/k8s/overlay-local.yaml"
kubectl apply -f "$SCRIPT_DIR/tms/k8s/deployment.yaml"
ok "tms manifests applied"

# ── 7. Apply external-scada/k8s manifests ───────────────────────────────
log "Applying external-scada/k8s manifests"
for f in 00-namespace.yaml 10-rabbitmq-configmap.yaml 20-rabbitmq-secret.yaml \
         30-rabbitmq-pvc.yaml 40-rabbitmq-deployment.yaml 50-rabbitmq-service.yaml \
         60-scada-api-secret.yaml 70-scada-api-deployment.yaml; do
  kubectl apply -f "$SCRIPT_DIR/external-scada/k8s/$f"
done
ok "scada manifests applied"

# ── 8. Patch imagePullPolicy + bridge probe paths/timeouts ──────────────
# imagePullPolicy=IfNotPresent → use loaded local images
# Probe paths use lightweight /readiness + /liveness (full /health JSON
# was timing out at 1s on minikube — the readiness component aggregates
# JMS + Kafka + RabbitMQ health checks, which can take 2-3s to compute).
log "Patching imagePullPolicy and bridge probe config"
kubectl -n pinkline patch deploy pas-scada-bridge --type=json \
  -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/actuator/health/liveness"},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/initialDelaySeconds","value":180},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/timeoutSeconds","value":5},
    {"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/failureThreshold","value":5},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/actuator/health/readiness"},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/initialDelaySeconds","value":120},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/timeoutSeconds","value":5},
    {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/failureThreshold","value":10}
  ]' 2>/dev/null \
  || kubectl -n pinkline patch deploy pas-scada-bridge --type=json \
       -p='[{"op":"add","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]' \
       2>/dev/null || true
kubectl -n scada patch deploy scada-api --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]' \
  2>/dev/null \
  || kubectl -n scada patch deploy scada-api --type=json \
       -p='[{"op":"add","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]' \
       2>/dev/null || true
ok "pull policies + probe timings patched"

# Force a rollout restart NOW so pods pick up freshly-loaded images with
# correct probe timings already in place. Triggers redeploy of the
# locally-built apps; no-op on first run when no rollout history exists.
log "Restarting locally-built deployments to pick up rebuilt images"
kubectl -n scada    rollout restart deploy/scada-api          2>/dev/null || true
kubectl -n pinkline rollout restart deploy/pas-scada-bridge   2>/dev/null || true
ok "rollout restarts kicked"

# ── 9. Wait for Kafka, then create topics ───────────────────────────────
log "Waiting for Kafka"
kubectl -n pinkline rollout status deploy/kafka --timeout=300s
kubectl -n pinkline wait --for=condition=ready pod -l app=kafka --timeout=300s
ok "Kafka ready"

log "Bootstrapping Kafka topics"
kubectl -n pinkline delete job bootstrap-kafka-topics --ignore-not-found
kubectl apply -f "$SCRIPT_DIR/bootstrap/k8s/10-kafka-topics-job.yaml"
# 480s — Kafka can need 1-2 min of post-Ready warmup before accepting
# new-topic create calls (controller election, metadata sync, etc.).
kubectl -n pinkline wait --for=condition=complete job/bootstrap-kafka-topics --timeout=480s
ok "topics ready"

# ── 10. Wait for RabbitMQ, declare scada queue + binding in-cluster ─────
log "Waiting for RabbitMQ"
kubectl -n scada rollout status deploy/rabbitmq --timeout=300s
kubectl -n scada wait --for=condition=ready pod -l app=rabbitmq --timeout=300s
ok "RabbitMQ ready"

log "Declaring scada.tms.alarms.queue + binding"
kubectl -n scada exec deploy/rabbitmq -- \
  rabbitmqctl --quiet --silent eval \
    '{ok, _} = rabbit_amqqueue:declare(rabbit_misc:r(<<"/">>, queue, <<"scada.tms.alarms.queue">>), true, false, [], none, <<"thiru">>), ok.' \
  >/dev/null 2>&1 || warn "rabbitmqctl declare returned non-zero (likely already exists — continuing)"
# Bind via the management HTTP API (ClusterIP) from a temporary pod.
kubectl -n scada run rmq-bind-$$ --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 -- sh -c '
    set -e
    BASE="http://rabbitmq-internal:15672/api"
    AUTH="-u thiru:password"
    curl -fsS $AUTH -X PUT \
      -H "Content-Type: application/json" \
      --data "{\"durable\":true,\"auto_delete\":false}" \
      "$BASE/queues/%2F/scada.tms.alarms.queue" >/dev/null
    curl -fsS $AUTH -X POST \
      -H "Content-Type: application/json" \
      --data "{\"routing_key\":\"scada.tms.alarms\"}" \
      "$BASE/bindings/%2F/e/amq.topic/q/scada.tms.alarms.queue" >/dev/null
    echo "queue + binding OK"
  ' || warn "queue/binding declare failed — verify with: kubectl -n scada exec deploy/rabbitmq -- rabbitmqctl list_queues"

# ── 11. Apply Monitor + Demo ────────────────────────────────────────────
log "Applying Monitor manifests"
kubectl apply -f "$SCRIPT_DIR/monitor/k8s/30-pvc.yaml"
kubectl apply -f "$SCRIPT_DIR/monitor/k8s/20-secret.yaml"
kubectl apply -f "$SCRIPT_DIR/monitor/k8s/overlay-local.yaml"
kubectl apply -f "$SCRIPT_DIR/monitor/k8s/40-deployment.yaml"
kubectl -n pinkline patch deploy pas-scada-monitor --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]' \
  2>/dev/null \
  || kubectl -n pinkline patch deploy pas-scada-monitor --type=json \
       -p='[{"op":"add","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]' \
       2>/dev/null || true
kubectl -n pinkline rollout restart deploy/pas-scada-monitor 2>/dev/null || true
ok "Monitor applied"

log "Applying Demo manifests"
kubectl apply -f "$SCRIPT_DIR/demo/k8s/10-configmap.yaml"
kubectl apply -f "$SCRIPT_DIR/demo/k8s/20-deployment.yaml"
kubectl -n pinkline patch deploy pas-scada-demo --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]' \
  2>/dev/null \
  || kubectl -n pinkline patch deploy pas-scada-demo --type=json \
       -p='[{"op":"add","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]' \
       2>/dev/null || true
kubectl -n pinkline rollout restart deploy/pas-scada-demo 2>/dev/null || true
ok "Demo applied"

# ── 12. Status + URLs ───────────────────────────────────────────────────
log "Final status"
kubectl -n pinkline get pods
echo
kubectl -n scada get pods
echo

log "Access URLs (port-forward locally to reach these)"
cat <<EOF

  Bridge:           kubectl -n pinkline port-forward svc/pas-scada-bridge 8085:8085
                    → http://localhost:8085/actuator/health
                    → http://localhost:8085/api/messages       (in-bridge monitor)
                    → http://localhost:8085/api/snapshot/replay (POST)

  Kafdrop:          kubectl -n pinkline port-forward svc/kafdrop 9000:9000
                    → http://localhost:9000

  Health monitor:   kubectl -n pinkline port-forward svc/pas-scada-monitor 8080:8080
                    → http://localhost:8080            (live up/down dashboard)
                    → http://localhost:8080/state      (JSON of all probes)

  Customer demo:    kubectl -n pinkline port-forward svc/pas-scada-demo 8090:8090
                    → http://localhost:8090            (live data table)
                    → http://localhost:8090/flow       (animated flow diagram)

  RabbitMQ admin:   kubectl -n scada port-forward svc/rabbitmq-internal 15672:15672
                    → http://localhost:15672  (thiru/password)

  RabbitMQ MQTT:    kubectl -n scada port-forward svc/rabbitmq-internal 1883:1883
                    → mqtt://localhost:1883  (thiru/password — for MQTT Explorer)

  SCADA API:        kubectl -n scada port-forward svc/scada-api-internal 8091:8091
                    → http://localhost:8091/api/status

  Artemis console:  http://localhost:8161  (admin/admin)

EOF
ok "Stack is up. Tear down with: kubectl delete ns pinkline scada && docker compose -f $MESSAGING_INFRA/docker-compose.yml down"
