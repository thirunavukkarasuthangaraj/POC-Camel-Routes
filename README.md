# PAS-SCADA Kafka Bridge

Pink Line TMS ↔ SCADA integration bridge.
Consumes XML messages from ActiveMQ Artemis, converts to JSON, encrypts with AES-256-GCM, and delivers to SCADA via Kafka → RabbitMQ → MQTT.

---

## Architecture

```
TMS Server (Artemis)
      │ XML over OpenWire
      ▼
  Bridge App  (Java · Spring Boot · Apache Camel)
  XML → JSON → AES-256-GCM encrypt
      │              │              │
   Kafka         RabbitMQ         MQTT
      │              │ AMQP          │
      └──────────────┼──────────────┘
                     ▼
              SCADA API (Python · Flask · Paho MQTT)
              Decrypt → Display → Publish RSAE responses
```

**Two VMs:**

| VM | IP | Runs |
|----|-----|------|
| TMS VM | `10.4.0.23` | Bridge · Kafka · Zookeeper · Kafdrop |
| SCADA VM | `10.4.0.25` | RabbitMQ · SCADA API |

---

## Quick Start

### Local (all-in-one, no VMs needed)
```bash
docker compose up -d
# Bridge:    http://localhost:8085/actuator/health
# Kafdrop:   http://localhost:9000
# RabbitMQ:  http://localhost:15672  (thiru / password)
# Artemis:   http://localhost:8161
```

### Production (2-VM deploy)
```bash
# TMS VM (10.4.0.23)
cd tms && docker compose up -d

# SCADA VM (10.4.0.25)
cd external-scada && docker compose up -d
# Dashboard: http://10.4.0.25:8091
```

---

## Environment Variables

Copy `.env.example` and fill in values:
```bash
cp .env.example .env
```

See `cicd/README.md` for full setup instructions.

---

## Key Ports

| Service | Port | Protocol |
|---------|------|----------|
| Bridge health | `8085` | HTTP |
| Artemis | `61616` | OpenWire |
| Kafka | `9092` | PLAINTEXT |
| Kafdrop UI | `9000` | HTTP |
| RabbitMQ AMQP | `5673` | AMQP |
| RabbitMQ MQTT | `1884` | MQTT |
| RabbitMQ UI | `15672` | HTTP |
| SCADA Dashboard | `8091` | HTTP |

---

## Docs

| File | Purpose |
|------|---------|
| `HOWTO-START.md` | Step-by-step setup & operations guide |
| `ARCHITECTURE.md` | System design & topology |
| `cicd/README.md` | CI/CD pipeline setup |
| `cairo-artemis.md` | External Artemis connection details |
| `RUNGUIDE.html` | Full operations reference |
| `DEBUGGUIDE.html` | Debugging guide |

---

## RSAE Message Types (SCADA → TMS)

| Type | When |
|------|------|
| `UpdateAlarm` | Equipment state change (auto every 10s) |
| `KeepAlive` | Heartbeat (auto every 30s) |
| `SendAllAlarms` | Full alarm broadcast (auto every 60s) |
| `GetAllAlarms` | Request TMS alarm state (auto every 120s) |
