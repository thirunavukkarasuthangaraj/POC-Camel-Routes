#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Rebuild the scada-api-static ConfigMap from local static/ files
# and restart the pod. Use after editing dashboard.html / power.html.
#
# Loop time: ~10s. NO docker build, NO image load.
#
#   ./external-scada/k8s/apply-static.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

NS="${NS:-scada}"
STATIC_DIR="$(dirname "$0")/../scada-api/static"

echo "→ Regenerating ConfigMap scada-api-static from $STATIC_DIR"
kubectl -n "$NS" create configmap scada-api-static \
  --from-file="$STATIC_DIR" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "→ Restarting deploy/scada-api"
kubectl -n "$NS" rollout restart deploy/scada-api
kubectl -n "$NS" rollout status  deploy/scada-api --timeout=120s

echo "✓ Done. Dashboard changes are live."
