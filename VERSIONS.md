# PAS–SCADA / TMS Messaging Integration — Technology Stack

This document summarises the messaging architecture, the protocols used
between systems, and the version of each component deployed in the
TMS ↔ SCADA integration.

## Architecture overview

The integration moves messages between the **TMS** side and the **SCADA**
side through a bridge, with each hop using the protocol native to that
system:

```
TMS  ──Artemis──▶  Bridge / Kafka  ──AMQP──▶  RabbitMQ  ──MQTT──▶  SCADA API
(Artemis broker)   (transform &              (message broker)     (alarm &
                    encrypt)                                        power view)
```

- **TMS → SCADA** messages are encrypted (AES-256-GCM) on the bridge and
  decrypted by the SCADA API.
- **SCADA → TMS** messages travel back as plain JSON (RSAE envelope).

## Protocols

| Hop | Protocol | Version |
|---|---|---|
| TMS broker ↔ Bridge | Artemis Core / STOMP | ActiveMQ Artemis 2.33.0 |
| Bridge ↔ RabbitMQ | **AMQP 0-9-1** | RabbitMQ 3.12 |
| SCADA API ↔ RabbitMQ | **MQTT 3.1.1** | RabbitMQ MQTT plugin |
| Browser SCADA viewer ↔ RabbitMQ | **MQTT over WebSocket** | RabbitMQ Web-MQTT plugin |
| Internal transport | Apache Kafka | 7.5.0 |

## Component versions

| Component | Role | Version |
|---|---|---|
| **RabbitMQ** | SCADA-side message broker (AMQP + MQTT) | **3.12** (management edition) |
| **MQTT** | SCADA messaging protocol | **3.1.1** (via RabbitMQ MQTT plugin) |
| **ActiveMQ Artemis** | TMS-side message broker | **2.33.0** |
| **Apache Kafka** | Streaming backbone | **7.5.0** |
| **Apache ZooKeeper** | Kafka coordination | **7.5.0** |
| **SCADA API** | Mock SCADA service (Python) | Python **3.12** |

## RabbitMQ — listeners & ports

| Port | Protocol | Purpose |
|---|---|---|
| `5672` | AMQP 0-9-1 | Bridge publishes inbound TMS messages |
| `1883` | MQTT (TCP) | SCADA API subscribes to alarms / power data |
| `15675` | MQTT over WebSocket | Browser-based SCADA viewer |
| `15672` | HTTP | Management console |

## MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `tms/scada/pas` | TMS → SCADA | Encrypted (AES-256-GCM) |
| `scada/tms/alarms` | SCADA → TMS | Plain JSON (RSAE envelope) |

## Security

- **Transport encryption:** TMS → SCADA payloads are AES-256-GCM encrypted.
- **Authentication:** MQTT and AMQP require credentials; anonymous access
  is disabled on the broker.

---
_PAS–SCADA–Kafka–Bridge · Technology Stack Summary · 2026-05-26_
