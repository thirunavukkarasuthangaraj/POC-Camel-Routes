# Azure Docker Deployment Guide
## PAS-SCADA-Kafka-Bridge — Pink Line Project


---

## Prerequisites

- Azure account with active subscription
- Azure CLI installed → https://learn.microsoft.com/en-us/cli/azure/install-azure-cli
- Docker Desktop installed and running
- Maven installed (for building JAR)

---

## Step 1 — Login to Azure

```bash
az login
```

Set your subscription (if you have multiple):
```bash
az account set --subscription "<your-subscription-id>"
```

---

## Step 2 — Create Resource Group

```bash
az group create \
  --name pinkline-rg \
  --location eastasia
```

> Use `eastasia` or `southeastasia` — closest to Pink Line project location.

---

## Step 3 — Create Azure Container Registry (ACR)

```bash
az acr create \
  --resource-group pinkline-rg \
  --name pinklineacr \
  --sku Basic \
  --admin-enabled true
```

Get the ACR login server:
```bash
az acr show --name pinklineacr --query loginServer --output tsv
# Output: pinklineacr.azurecr.io
```

---

## Step 4 — Build the JAR

```bash
mvn clean package -DskipTests
```

---

## Step 5 — Build & Push Docker Image to ACR

Login to ACR:
```bash
az acr login --name pinklineacr
```

Build the image:
```bash
docker build -t pinklineacr.azurecr.io/pas-scada-bridge:latest .
```

Push to ACR:
```bash
docker push pinklineacr.azurecr.io/pas-scada-bridge:latest
```

Verify it's uploaded:
```bash
az acr repository list --name pinklineacr --output table
```

---

## Step 6 — Create Azure Key Vault (for Secrets)

```bash
az keyvault create \
  --name pinkline-kv \
  --resource-group pinkline-rg \
  --location eastasia
```

Store secrets (never put these in config files):
```bash
az keyvault secret set --vault-name pinkline-kv --name ARTEMIS-USER  --value "pasbridge"
az keyvault secret set --vault-name pinkline-kv --name ARTEMIS-PASS  --value "<real-password>"
az keyvault secret set --vault-name pinkline-kv --name KAFKA-USER    --value "tms_bridge"
az keyvault secret set --vault-name pinkline-kv --name KAFKA-PASS    --value "<real-password>"
az keyvault secret set --vault-name pinkline-kv --name RABBITMQ-USER --value "tms_bridge"
az keyvault secret set --vault-name pinkline-kv --name RABBITMQ-PASS --value "<real-password>"
az keyvault secret set --vault-name pinkline-kv --name MQTT-USER     --value "tms_bridge"
az keyvault secret set --vault-name pinkline-kv --name MQTT-PASS     --value "<real-password>"
az keyvault secret set --vault-name pinkline-kv --name SCADA-AES-KEY --value "<base64-256bit-key>"
```

---

## Step 7 — Deploy to Azure Container Instances (ACI)

> ACI is the simplest option — no Kubernetes needed for single container.

```bash
az container create \
  --resource-group pinkline-rg \
  --name pas-scada-bridge \
  --image pinklineacr.azurecr.io/pas-scada-bridge:latest \
  --registry-login-server pinklineacr.azurecr.io \
  --registry-username pinklineacr \
  --registry-password $(az acr credential show --name pinklineacr --query passwords[0].value -o tsv) \
  --cpu 1 \
  --memory 1 \
  --ports 8085 \
  --environment-variables \
      SPRING_PROFILES_ACTIVE=prod \
      ARTEMIS_HOST=10.12.1.13 \
      ARTEMIS_PORT=61616 \
      KAFKA_HOST=10.12.1.14 \
      KAFKA_PORT=9092 \
      RABBITMQ_HOST=10.12.1.11 \
      RABBITMQ_PORT=5672 \
      MQTT_HOST=10.12.1.11 \
      MQTT_PORT=8883 \
      BRIDGE_PIPELINE=kafka,rabbitmq \
      BRIDGE_MONITOR_ENABLED=true \
      BRIDGE_MONITOR_FROM_EXCHANGE=amq.topic \
      BRIDGE_MONITOR_ROUTING_KEY=tms.scada.pas \
      BRIDGE_MONITOR_QUEUE=scada.monitor.queue \
  --secure-environment-variables \
      ARTEMIS_USER=$(az keyvault secret show --vault-name pinkline-kv --name ARTEMIS-USER --query value -o tsv) \
      ARTEMIS_PASS=$(az keyvault secret show --vault-name pinkline-kv --name ARTEMIS-PASS --query value -o tsv) \
      KAFKA_USER=$(az keyvault secret show --vault-name pinkline-kv --name KAFKA-USER --query value -o tsv) \
      KAFKA_PASS=$(az keyvault secret show --vault-name pinkline-kv --name KAFKA-PASS --query value -o tsv) \
      RABBITMQ_USER=$(az keyvault secret show --vault-name pinkline-kv --name RABBITMQ-USER --query value -o tsv) \
      RABBITMQ_PASS=$(az keyvault secret show --vault-name pinkline-kv --name RABBITMQ-PASS --query value -o tsv) \
      MQTT_USER=$(az keyvault secret show --vault-name pinkline-kv --name MQTT-USER --query value -o tsv) \
      MQTT_PASS=$(az keyvault secret show --vault-name pinkline-kv --name MQTT-PASS --query value -o tsv) \
      SCADA_AES_KEY=$(az keyvault secret show --vault-name pinkline-kv --name SCADA-AES-KEY --query value -o tsv)
```

---

## Step 8 — Verify Deployment

Check container status:
```bash
az container show \
  --resource-group pinkline-rg \
  --name pas-scada-bridge \
  --query instanceView.state \
  --output tsv
```

View logs:
```bash
az container logs \
  --resource-group pinkline-rg \
  --name pas-scada-bridge
```

Get public IP:
```bash
az container show \
  --resource-group pinkline-rg \
  --name pas-scada-bridge \
  --query ipAddress.ip \
  --output tsv
```

Health check:
```bash
curl http://<public-ip>:8085/actuator/health
```

Stream API:
```bash
curl http://<public-ip>:8085/api/messages/stream
```

---

## Step 9 — Update Image (Redeploy)

When code changes:

```bash
# 1. Build new JAR
mvn clean package -DskipTests

# 2. Build & push new image
docker build -t pinklineacr.azurecr.io/pas-scada-bridge:latest .
docker push pinklineacr.azurecr.io/pas-scada-bridge:latest

# 3. Restart container to pull new image
az container restart \
  --resource-group pinkline-rg \
  --name pas-scada-bridge
```

---

## Option B — Deploy to Azure Kubernetes Service (AKS)

> Use AKS if you need high availability, auto-scaling, or already have K8s manifests.

### Create AKS cluster

```bash
az aks create \
  --resource-group pinkline-rg \
  --name pinkline-aks \
  --node-count 1 \
  --node-vm-size Standard_B2s \
  --attach-acr pinklineacr \
  --generate-ssh-keys
```

Connect kubectl:
```bash
az aks get-credentials \
  --resource-group pinkline-rg \
  --name pinkline-aks
```

### Apply K8s manifests

Create namespace:
```bash
kubectl create namespace pinkline
```

Apply ConfigMap and Secret (update values for production first):
```bash
kubectl apply -f tms/k8s/configmap.yaml
kubectl apply -f tms/k8s/secret.yaml
```

Deploy the bridge:
```bash
kubectl apply -f k8s/
```

Check pods:
```bash
kubectl get pods -n pinkline
kubectl logs -n pinkline deployment/pas-scada-bridge
```

---

## Environment Variables Reference

| Variable | Description | Example |
|---|---|---|
| `SPRING_PROFILES_ACTIVE` | Spring profile | `prod` |
| `ARTEMIS_HOST` | Artemis server IP | `10.12.1.13` |
| `ARTEMIS_PORT` | Artemis OpenWire port | `61616` |
| `ARTEMIS_USER` | Artemis username | `pasbridge` |
| `ARTEMIS_PASS` | Artemis password | *(from Key Vault)* |
| `KAFKA_HOST` | Kafka broker IP | `10.12.1.14` |
| `KAFKA_PORT` | Kafka port | `9092` |
| `RABBITMQ_HOST` | RabbitMQ IP | `10.12.1.11` |
| `RABBITMQ_PORT` | RabbitMQ AMQP port | `5672` |
| `RABBITMQ_USER` | RabbitMQ username | `tms_bridge` |
| `RABBITMQ_PASS` | RabbitMQ password | *(from Key Vault)* |
| `MQTT_HOST` | MQTT broker IP | `10.12.1.11` |
| `MQTT_PORT` | MQTT port | `8883` (TLS prod) |
| `MQTT_USER` | MQTT username | `tms_bridge` |
| `MQTT_PASS` | MQTT password | *(from Key Vault)* |
| `SCADA_AES_KEY` | AES-256 key (base64) | *(from Key Vault)* |
| `BRIDGE_PIPELINE` | Delivery chain | `kafka,rabbitmq` |
| `BRIDGE_MONITOR_ENABLED` | Enable /api/messages | `true` |

---

## Quick Reference

```
ACR Image  : pinklineacr.azurecr.io/pas-scada-bridge:latest
Health     : http://<ip>:8085/actuator/health
Stream API : http://<ip>:8085/api/messages/stream
All msgs   : http://<ip>:8085/api/messages
```

---

## Production Checklist

- [ ] Real AES-256-GCM key generated and stored in Key Vault
- [ ] All passwords stored in Key Vault — not in config files
- [ ] MQTT port changed to `8883` with TLS enabled
- [ ] Kafka SASL/SSL enabled
- [ ] RabbitMQ TLS enabled
- [ ] ACR image tagged with version (not just `latest`)
- [ ] AKS node count set for HA (minimum 2)
- [ ] Health check endpoint monitored
