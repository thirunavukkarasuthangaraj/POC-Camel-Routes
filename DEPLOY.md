# Deployment Guide — PAS-SCADA-Kafka-Bridge

End-to-end runbook for deploying the integration platform to a Kubernetes
cluster. Covers prod deploy, image build/push, verification, rollback, and
the minikube dev variant.

---

## Architecture (deploy-time view)

Two namespaces, three application deployments:

```
                    [ pinkline ns ]                    [ scada ns ]
   TMS apps ──► Artemis ──► Bridge ──► Kafka ──► RabbitMQ ──► SCADA API
                              │                       ▲
                              └─► Monitor             │
                                                  (MQTT plugin)
```

| Deployment | Namespace | Image | Purpose |
|------------|-----------|-------|---------|
| `pas-scada-bridge` | `pinkline` | `pinkline/pas-scada-bridge` | Camel routes — Artemis ⇄ Kafka ⇄ RabbitMQ |
| `pas-scada-monitor` | `pinkline` | `pinkline/pas-scada-monitor` | Health monitor + email alerts |
| `rabbitmq` | `scada` | `rabbitmq:3.12-management` | AMQP + MQTT broker |
| `scada-api` | `scada` | `pinkline/pas-scada-api` | Mock SCADA simulator |
| `kafka-connect` (optional) | `pinkline` | `pinkline/pas-scada-connect` | Kafka Connect cluster (Phase 2) |

---

## Prerequisites

```bash
# Tools
kubectl >= 1.28
docker (for image builds)
git
# Access
- A Kubernetes cluster with kubectl context configured
- A container registry the cluster can pull from (GHCR, ECR, GCR, ACR, Harbor, …)
```

Verify cluster access:

```bash
kubectl config current-context
kubectl get nodes
```

---

## 1 · Build & push images

Build once per release. Tag with semantic version, **never use `:latest` in prod**.

```bash
export REGISTRY=ghcr.io/<your-org>      # or your registry URL
export VERSION=v1.0.0

cd PAS-SCADA-Kafka-Bridge

# Bridge (Camel routes)
docker build -t $REGISTRY/pinkline/pas-scada-bridge:$VERSION   tms/

# SCADA API (mock simulator)
docker build -t $REGISTRY/pinkline/pas-scada-api:$VERSION      external-scada/scada-api/

# Monitor (FastAPI health screen)
docker build -t $REGISTRY/pinkline/pas-scada-monitor:$VERSION  monitor/

# Kafka Connect (optional)
docker build -t $REGISTRY/pinkline/pas-scada-connect:$VERSION  connect/

# Push all
docker push $REGISTRY/pinkline/pas-scada-bridge:$VERSION
docker push $REGISTRY/pinkline/pas-scada-api:$VERSION
docker push $REGISTRY/pinkline/pas-scada-monitor:$VERSION
docker push $REGISTRY/pinkline/pas-scada-connect:$VERSION
```

---

## 2 · Update committed manifests for your environment

Before first deploy, edit these files **once per environment** and commit:

### Image references

| File | Edit |
|------|------|
| `tms/k8s/deployment.yaml` | `image: $REGISTRY/pinkline/pas-scada-bridge:$VERSION` |
| `external-scada/k8s/70-scada-api-deployment.yaml` | `image: $REGISTRY/pinkline/pas-scada-api:$VERSION` |
| `monitor/k8s/40-deployment.yaml` | `image: $REGISTRY/pinkline/pas-scada-monitor:$VERSION` |
| `connect/k8s/30-deployment.yaml` | `image: $REGISTRY/pinkline/pas-scada-connect:$VERSION` |

### Secrets — **DO NOT COMMIT PLAINTEXT**

For production, replace the placeholder Secret manifests with one of:

- **SealedSecrets** (Bitnami) — encrypts secrets in git, decrypts in cluster
- **External Secrets Operator** + Vault / AWS Secrets Manager / Azure KeyVault
- `kubectl create secret …` ad-hoc (not git-tracked)

Files holding secrets:

```
tms/k8s/secret.yaml                       ← bridge: Artemis, RabbitMQ, MQTT, AES
external-scada/k8s/20-rabbitmq-secret.yaml ← RabbitMQ user/pass + Erlang cookie
external-scada/k8s/60-scada-api-secret.yaml ← SCADA API: AES, MQTT
monitor/k8s/20-secret.yaml                 ← SMTP, RabbitMQ
connect/k8s/10-secret.yaml                 ← Connect: Artemis, RabbitMQ, MQTT
```

The **AES key must be identical** in `tms/k8s/secret.yaml` and
`external-scada/k8s/60-scada-api-secret.yaml` — bridge encrypts, SCADA API
decrypts with the same key. Generate via `openssl rand -base64 32`.

---

## 3 · Deploy

### TMS side (`pinkline` namespace) — Artemis, Kafka, Zookeeper, Bridge, Monitor

```bash
kubectl apply -f tms/k8s/00-namespace.yaml
kubectl apply -f tms/k8s/10-artemis.yaml
kubectl apply -f tms/k8s/20-zookeeper.yaml
kubectl apply -f tms/k8s/30-kafka.yaml

# Wait for brokers to be ready before deploying the bridge
kubectl rollout status statefulset/artemis -n pinkline --timeout=300s
kubectl rollout status statefulset/kafka -n pinkline --timeout=300s
kubectl rollout status statefulset/zookeeper -n pinkline --timeout=300s

# Bridge
kubectl apply -f tms/k8s/configmap.yaml
kubectl apply -f tms/k8s/secret.yaml
kubectl apply -f tms/k8s/deployment.yaml

# Monitor
kubectl apply -f monitor/k8s/10-configmap.yaml
kubectl apply -f monitor/k8s/20-secret.yaml
kubectl apply -f monitor/k8s/30-pvc.yaml
kubectl apply -f monitor/k8s/40-deployment.yaml
```

### SCADA side (`scada` namespace) — RabbitMQ + SCADA API

```bash
kubectl apply -f external-scada/k8s/00-namespace.yaml
kubectl apply -f external-scada/k8s/10-rabbitmq-configmap.yaml
kubectl apply -f external-scada/k8s/20-rabbitmq-secret.yaml
kubectl apply -f external-scada/k8s/30-rabbitmq-pvc.yaml
kubectl apply -f external-scada/k8s/40-rabbitmq-deployment.yaml
kubectl apply -f external-scada/k8s/50-rabbitmq-service.yaml

kubectl rollout status deployment/rabbitmq -n scada --timeout=300s

kubectl apply -f external-scada/k8s/60-scada-api-secret.yaml
kubectl apply -f external-scada/k8s/70-scada-api-deployment.yaml
```

### Kafka Connect (optional, Phase 2)

> ⚠️ As of v1.0.0 the Camel kafka-connector plugins do not register in
> Confluent Connect 7.5 (class-loading issue). Skip this section unless
> using a connector library known to work with Connect 7.5
> (e.g. Confluent or Lenses RabbitMQ connectors).

```bash
kubectl apply -f connect/k8s/10-secret.yaml
kubectl apply -f connect/k8s/20-configmap.yaml
kubectl apply -f connect/k8s/30-deployment.yaml
kubectl rollout status deployment/kafka-connect -n pinkline --timeout=600s
kubectl apply -f connect/k8s/40-job-register.yaml
```

### Verify

```bash
kubectl get pods -A | grep -E "pinkline|scada"
# All pods should be Running 1/1
```

---

## 4 · Smoke test

```bash
# Port-forward each service
kubectl port-forward -n pinkline svc/pas-scada-bridge   8085:8085 &
kubectl port-forward -n pinkline svc/pas-scada-monitor  8080:8080 &
kubectl port-forward -n scada    svc/scada-api-internal 8091:8091 &
kubectl port-forward -n scada    svc/rabbitmq-internal  15672:15672 &

# Bridge health (expect status:UP, jms:UP, rabbit:UP)
curl http://localhost:8085/actuator/health

# Monitor screen
open http://localhost:8080/                         # browser

# SCADA API dashboard
open http://localhost:8091/                         # browser

# RabbitMQ Management
open http://localhost:15672/                        # browser

# Send a test message via Artemis CLI
kubectl exec -n pinkline statefulset/artemis -- \
  /var/lib/artemis-instance/bin/artemis producer \
    --destination "topic://TMS.PISInfo" \
    --user admin --password admin \
    --message-count 1 \
    --message '<TmsPisInfo><id>SMOKE-1</id><text>hello</text></TmsPisInfo>'

# Verify it landed at SCADA API
curl http://localhost:8091/api/received | head -c 300
```

---

## 5 · Rollback

```bash
# View revision history
kubectl rollout history deployment/pas-scada-bridge -n pinkline

# Roll back one revision
kubectl rollout undo deployment/pas-scada-bridge -n pinkline

# Roll back to specific revision
kubectl rollout undo deployment/pas-scada-bridge -n pinkline --to-revision=N

# Same for other deployments
kubectl rollout undo deployment/scada-api -n scada
kubectl rollout undo deployment/pas-scada-monitor -n pinkline
```

For brokers (StatefulSets), rollback is the same:

```bash
kubectl rollout undo statefulset/artemis -n pinkline
kubectl rollout undo statefulset/kafka -n pinkline
```

---

## 6 · Scaling

Bridge and SCADA API are stateless — scale freely:

```bash
kubectl scale deployment/pas-scada-bridge -n pinkline --replicas=3
kubectl scale deployment/scada-api -n scada --replicas=2
```

> Monitor must stay at `replicas=1` (state file on PVC, single writer).
> Brokers should be scaled by editing the StatefulSet `replicas` and
> ensuring proper Kafka replication factor / Zookeeper ensemble size.

---

## 7 · Tear-down

```bash
# Drop all app workloads (keeps PVCs)
kubectl delete deployment,statefulset,service,configmap,secret \
  --all -n pinkline
kubectl delete deployment,statefulset,service,configmap,secret \
  --all -n scada

# Wipe data too (DESTRUCTIVE — will delete all messages, alarm state)
kubectl delete pvc --all -n pinkline
kubectl delete pvc --all -n scada

# Drop namespaces
kubectl delete ns pinkline scada
```

---

## Minikube / dev environment

The **`*/k8s/overlay-minikube.yaml`** files are dev-only overrides used
when Kafka/Artemis/Zookeeper run as Docker containers (via
`messaging-infra/docker-compose.yml`) instead of in-cluster.

These overlays point env vars at `host.minikube.internal:9092` /
`:61616` so pods can reach the Docker brokers from inside minikube.

```bash
# 1. Start brokers in Docker
cd ../messaging-infra
docker compose up -d

# 2. Apply k8s manifests INCLUDING the overlay
cd ../PAS-SCADA-Kafka-Bridge
kubectl apply -f tms/k8s/00-namespace.yaml
kubectl apply -f tms/k8s/overlay-minikube.yaml      # ← override
kubectl apply -f tms/k8s/deployment.yaml

kubectl apply -f external-scada/k8s/                # full RabbitMQ + scada-api
kubectl apply -f monitor/k8s/30-pvc.yaml
kubectl apply -f monitor/k8s/overlay-minikube.yaml  # ← override
kubectl apply -f monitor/k8s/40-deployment.yaml
```

> **Real prod uses `configmap.yaml` + `secret.yaml`, NOT the overlay.**
> The overlay is identified clearly by the `overlay-minikube.yaml` filename
> and the `host.minikube.internal` host references inside.

---

## Common operational tasks

### Tail logs

```bash
kubectl logs -n pinkline deploy/pas-scada-bridge --tail=100 -f
kubectl logs -n scada deploy/scada-api --tail=100 -f
```

### Restart a deployment (rolling)

```bash
kubectl rollout restart deployment/pas-scada-bridge -n pinkline
```

### Edit ConfigMap then reload

```bash
kubectl edit configmap bridge-config -n pinkline
kubectl rollout restart deployment/pas-scada-bridge -n pinkline
```

### Inspect Kafka topics

```bash
kubectl exec -n pinkline statefulset/kafka -- \
  kafka-topics --bootstrap-server localhost:9092 --list
```

### Get messages off a topic

```bash
kubectl exec -n pinkline statefulset/kafka -- \
  kafka-console-consumer --bootstrap-server localhost:9092 \
    --topic tms.scada.encrypted --from-beginning --max-messages 5
```

### RabbitMQ queue stats

```bash
kubectl exec -n scada deploy/rabbitmq -- rabbitmqctl list_queues name messages
```

### Force a connector reconcile (Connect)

```bash
kubectl delete job kafka-connect-register -n pinkline --ignore-not-found
kubectl apply -f connect/k8s/40-job-register.yaml
```

---

## Pre-prod checklist

Before flipping prod traffic:

- [ ] Real container registry, real image tags (no `:latest`)
- [ ] Secrets sourced from Vault / SealedSecrets / External Secrets — not committed plaintext
- [ ] Production AES key generated fresh; same key in bridge and SCADA API secrets
- [ ] TLS enabled on Artemis, Kafka, RabbitMQ, MQTT (uncomment TLS sections in `application-prod.properties` and supply truststore secret)
- [ ] `replicationFactor=3` on Kafka topics, `min.insync.replicas=2`, 3 Kafka brokers
- [ ] RabbitMQ clustered or mirrored queues for HA
- [ ] Monitor SMTP credentials configured and a test alert sent
- [ ] Pod resource requests/limits reviewed against load tests
- [ ] PodDisruptionBudgets in place for brokers (don't lose quorum during node drain)
- [ ] NetworkPolicies restricting inter-namespace traffic
- [ ] Backup strategy: Artemis journal, Kafka topic snapshots, RabbitMQ definitions
- [ ] Run a chaos test: kill a broker pod, verify no message loss, recovery time acceptable
