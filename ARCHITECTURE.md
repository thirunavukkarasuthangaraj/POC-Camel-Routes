# PAS-SCADA-Kafka-Bridge — Architecture & How It Works

Pink Line Project · TMS → SCADA Integration · Solution 4 · Client Confirmed · Bala Approved

---

## Box Model — Two-VM Deployment (Updated)

Aligned with **RSAE RabbitMQ Message Interface Specification**
(P-ST-00-0000-VV-ET-THS-000136-00 — Thales Portugal, Rev 01, 10/04/2018)

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                       INTERNAL VM  (TMS side — trusted zone)                     ║
║                                                                                  ║
║   ┌─────────┐   XML     ┌────────────────────────────────────────────────────┐   ║
║   │   TMS   │ ────────▶ │  ActiveMQ Artemis  (port 61616)                    │   ║
║   │ System  │           │                                                    │   ║
║   └─────────┘           │  Topics:                                           │   ║
║                         │   • TMS.PISInfo           (ATRTimeTable V3)        │   ║
║                         │   • RCS.E2K.TMS.TrafficReportClient                │   ║
║                         │   • RCS.E2K.TMS.RouteInfo                          │   ║
║                         │   • TSInfo                                         │   ║
║                         │   • Thales.EPS.ExternalSystems   (to add — EPS)    │   ║
║                         └─────────────────────┬──────────────────────────────┘   ║
║                              subscribers      │                                  ║
║                         ┌─────────────────────┴──────────────────────┐           ║
║                         ▼                                            ▼           ║
║              ┌─────────────────────┐              ┌──────────────────────────┐   ║
║              │  PASInfoConverter   │              │ PAS-SCADA-Kafka-Bridge   │   ║
║              │  (GP Product)       │              │ (this project)           │   ║
║              │  ✅ UNTOUCHED       │              │ Spring Boot + Camel      │   ║
║              └─────────────────────┘              └─────────────┬────────────┘   ║
║                                                                 │                ║
║   ┌─────────────────────────────────────────────────────────────┴────────────┐   ║
║   │  Bridge pipeline (config-driven: bridge.pipeline=kafka,rabbitmq)         │   ║
║   │                                                                          │   ║
║   │   from(artemis) ─▶ XmlToJson ─▶ [Encrypt*] ─▶ to(kafka) ─▶ to(rabbitmq) │   ║
║   │                                                                          │   ║
║   │   * bridge.encrypt.enabled  (currently true — AES-256-GCM)              │   ║
║   │   ⚠ RSAE spec expects PLAIN JSON — confirm with Bala before enabling    │   ║
║   │     encryption for EPS-targeted messages                                 │   ║
║   └──────────────────────────┬───────────────────────────────────────────────┘   ║
║                              │                                                   ║
║                  ┌───────────┴────────────┐                                      ║
║                  ▼                        ▼                                      ║
║         ┌──────────────────┐   (consumed via AMQP — cross-VM below)              ║
║         │  Kafka (9092)    │                                                     ║
║         │  topic:          │                                                     ║
║         │  tms.scada.      │   Durable audit buffer — survives downstream        ║
║         │  encrypted       │   outages. Kafdrop UI on 9000.                      ║
║         └──────────────────┘                                                     ║
║                                                                                  ║
║   Bridge REST/SSE API (port 8085):                                               ║
║     GET  /api/messages          — buffered JSON list                             ║
║     GET  /api/messages/stream   — live SSE stream                                ║
║     GET  /actuator/health, /actuator/camelroutes                                 ║
╚══════════════════════════════════════════════════════════════════════════════════╝
                               │
                               │  AMQP 0-9-1  port 5672
                               │  (bridge initiates — single firewall rule)
                               ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║                      EXTERNAL VM  (SCADA side — DMZ)                             ║
║                                                                                  ║
║   ┌──────────────────────────────────────────────────────────────────────────┐   ║
║   │  RabbitMQ Broker  (5672 AMQP · 1883 MQTT · 15672 admin · 15675 WS-MQTT)  │   ║
║   │                                                                          │   ║
║   │   ┌─────────────────────────────────────────────────────────────────┐    │   ║
║   │   │  Exchange: amq.topic    (topic exchange)                        │    │   ║
║   │   │                                                                 │    │   ║
║   │   │   Bindings per RSAE spec §3.1.3:                                │   ║
║   │   │     routing key   efacec.#       ──▶ Thales.EPS.                │   ║
║   │   │                                       ExternalSystems.Queue     │   ║
║   │   │     routing key   tms.scada.pas  ──▶ SCADA MQTT consumer        │   ║
║   │   │                                       (PAS-SCADA-Kafka-Bridge)  │   ║
║   │   └───────┬─────────────────────────────────────────┬───────────────┘    │   ║
║   │           │                                         │                    │   ║
║   │           ▼                                         ▼                    │   ║
║   │   ┌────────────────────────────────┐   ┌──────────────────────────┐      │   ║
║   │   │  MQTT Plugin                   │   │ Thales.EPS.ExternalSyst. │      │   ║
║   │   │  AMQP tms.scada.pas            │   │ Queue                    │      │   ║
║   │   │    → MQTT tms/scada/pas        │   │ (durable — EPS consumer) │      │   ║
║   │   └───────────────┬────────────────┘   └──────────────────────────┘      │   ║
║   └───────────────────│───────────────────────────────│──────────────────────┘   ║
║                       │  MQTT 1883 (local)            │  AMQP (local)            ║
║                       ▼                               ▼                          ║
║   ┌──────────────────────────────────┐  ┌────────────────────────────────────┐   ║
║   │  SCADA API Service               │  │  Thales EPS                        │   ║
║   │  subscribes MQTT tms/scada/pas   │  │  (Enhanced Public Security)        │   ║
║   │  [Decrypt if encryption enabled] │  │                                    │   ║
║   │  → ScateX / HMIs                 │  │  Publisher: exchange Thales.EPS    │   ║
║   └──────────────────────────────────┘  │             routing  Thales.EPS    │   ║
║                                         │             CreatorId: EPS         │   ║
║                                         │                                    │   ║
║                                         │  Consumes RSAE messages:           │   ║
║                                         │    • GetAllAlarms                  │   ║
║                                         │    • SendAllAlarms                 │   ║
║                                         │    • UpdateAlarm                   │   ║
║                                         │    • KeepAlive  (heartbeat)        │   ║
║                                         └────────────────────────────────────┘   ║
╚══════════════════════════════════════════════════════════════════════════════════╝

Firewall (one rule): INTERNAL_VM_IP → EXTERNAL_VM_IP : 5672/tcp
All other ports are VM-local.
```

### RSAE JSON Envelope (RabbitMQ Message Interface §2.3)

All messages on `Thales.EPS.ExternalSystems.Queue` use this shape:

```json
{
  "CreatorId": "ScateX",
  "Type":      "UpdateAlarm",
  "Timestamp": "2017-09-25 15:40:30.111",
  "Alarm": {
    "Timestamp": "2017-09-30 15:40:30.111",
    "Id":        "ESC_ROL_1_23",
    "State":     "0"
  }
}
```

| Field       | Type   | Notes                                                |
| ----------- | ------ | ---------------------------------------------------- |
| `CreatorId` | string | Originator system name (e.g. `EPS`, `ScateX`)        |
| `Type`      | string | `GetAllAlarms` / `SendAllAlarms` / `UpdateAlarm` / `KeepAlive` |
| `Timestamp` | string | `YYYY-MM-DD HH:mm:ss.fff`                            |
| `Alarm(s)`  | obj/[] | Present for `SendAllAlarms` / `UpdateAlarm` only     |
| `Alarm.Id`  | string | e.g. `ESC_ROL_1_23`                                  |
| `Alarm.State` | string | `0`=Off, `1`=On (other states possible)            |

### Reverse flow (SCADA → TMS) — SAME CROSS-VM LINK

```
SCADA API ──MQTT──▶ RabbitMQ ──(bridge pulls)──▶ Artemis ──▶ TMS
         (ext VM)              AMPQ 5672           (int VM)

Bridge route: KafkaBridgeRoutes.java:180-191
Config:       bridge.inbound[n].*  (currently commented out)
```

The bridge always initiates — no second firewall rule needed.

---

## What Each Box Does

### Box 1 — TMS System
- Train Management System on the Pink Line
- Automatically publishes XML messages to ActiveMQ Artemis topics
- **No code changes needed here**

### Box 2 — ActiveMQ Artemis (`10.12.1.13:61616`)
- Message broker already running at the client site
- Receives XML from TMS and distributes to all subscribers
- Both PASInfoConverter AND our bridge subscribe — each gets an independent copy
- Protocol: TCP/OpenWire, port 61616
- **No code changes needed here**

### Box 3 — PASInfoConverter (GP Product)
- Original General Product from the vendor
- Subscribes to same Artemis topics
- **ZERO changes — completely untouched**
- GP reviewer (GD) requirement fully satisfied

### Box 4 — PAS-SCADA-Kafka-Bridge *(your new code)*
- New Spring Boot service running alongside PASInfoConverter
- Contains two Camel routes:

#### Route 1: ArtemisToKafkaRoute
```
from(artemis topic)
  → XmlToJsonProcessor   [XML → ICD JSON via Jackson — automatic]
  → EncryptProcessor     [JSON → AES-256-GCM byte[]]
  → to(kafka topic)
```

#### Route 2: KafkaToRabbitRoute
```
from(kafka topic)
  → to(rabbitmq exchange)   [forwards encrypted bytes as-is]
```

### Box 5 — Apache Kafka (internal VM, port 9092)
- Durable audit buffer — holds messages if downstream outages occur
- Topic: `tms.scada.encrypted`
- Inspected via Kafdrop UI on port 9000
- Lives on **internal VM** only (not exposed across VMs)

### Box 6 — RabbitMQ (external VM, ports 5672/1883/15672/15675)
- The **cross-VM boundary** — bridge on internal VM publishes here over AMQP 5672
- Bindings per RSAE spec §3.1.3:
  - `amq.topic` + routing key `efacec.#` → queue `Thales.EPS.ExternalSystems.Queue`  (SCADA → EPS)
  - `amq.topic` + routing key `tms.scada.pas` → MQTT consumers  (TMS → SCADA)
- MQTT plugin converts AMQP routing keys (`.`) → MQTT topics (`/`)
- Web MQTT (15675) for browser-based SCADA viewers

### Box 7 — SCADA API Service (external VM)
- Subscribes MQTT `tms/scada/pas` — forwards to ScateX / HMIs
- If encryption is enabled upstream, this service holds the decrypt key
- Container placeholder in `docker-compose.external.yml` (commented, awaiting real image)

### Box 8 — Thales EPS (external VM or adjacent)
- **EPS** = Enhanced Public Security (per RSAE spec §1.3)
- Consumes alarm messages from `Thales.EPS.ExternalSystems.Queue`
- Publishes its own events to exchange `Thales.EPS` (routing key `Thales.EPS`, CreatorId `EPS`)
- Message types: `GetAllAlarms`, `SendAllAlarms`, `UpdateAlarm`, `KeepAlive`
- Uses `RabbitMQConfiguration.xml` to map `ExternalId` → internal `TriggerId`

### Deployment files
| File                             | Role                                    |
| -------------------------------- | --------------------------------------- |
| `docker-compose.internal.yml`    | Internal VM — Artemis, Kafka, Bridge    |
| `docker-compose.external.yml`    | External VM — RabbitMQ, SCADA API stub  |
| `docker-compose.yml`             | Legacy single-VM all-in-one (dev/test)  |
| `k8s/*.yaml`                     | Kubernetes manifests (alt. deployment)  |

---

## XML Formats Handled (from real PASInfoConverter source)

### ATRTimeTable — topic `TMS.PISInfo`
```xml
<ATRTimeTable>
  <dateTime>20260411T140000</dateTime>
  <Trains>
    <Tg>
      <TTGUID>dc-occ.eclrt-train-6250-guid</TTGUID>
      <TripNo>678</TripNo>
      <CTD lpid="101" tn="678"/>
      <Evts F="3" Id="2201" As="3600" Ds="3660"/>
    </Tg>
  </Trains>
</ATRTimeTable>
```
- `Evts.F` = flags bitmask: `0x01`=arrival valid, `0x02`=departure valid, `0x04`=history (skip)
- `As` = arrival seconds from `dateTime`, `Ds` = departure seconds

### SingleArrival — topic `RCS.E2K.TMS.TrafficReportClient`
```xml
<SingleArrival>
  <Arr>
    <train><TTGUID>guid</TTGUID></train>
    <loc><tmsid>2201</tmsid></loc>
    <atimes><oTime>20260411T143000</oTime></atimes>
  </Arr>
</SingleArrival>
```

### SingleDeparture — topic `RCS.E2K.TMS.TrafficReportClient`
```xml
<SingleDeparture>
  <Dep>
    <train><TTGUID>guid</TTGUID></train>
    <loc><tmsid>2201</tmsid></loc>
    <dtimes><oTime>20260411T143300</oTime></dtimes>
  </Dep>
</SingleDeparture>
```

### routeinfo — topic `RCS.E2K.TMS.RouteInfo`
```xml
<routeinfo>
  <TTGUID>guid</TTGUID>
  <dests>
    <dest tmsid="2201"/>
    <dest tmsid="2202"/>
  </dests>
</routeinfo>
```

---

## ICD JSON Output (PL-ICD-SCADA-JSON-001 Rev 0.1)

```json
{
  "schemaVersion": "1.0",
  "messageType": "TMS_PAS_UPDATE",
  "timestamp": "2026-04-11T14:00:00.000Z",
  "header": {
    "server": "RCS.E2K.PIS",
    "version": "1.0",
    "health": 1,
    "healthSeq": 47,
    "statusUpdateAll": false
  },
  "trains": [],
  "platformPredictions": [
    {
      "platformId": "PL2201",
      "predictedTrains": [
        {
          "slot": 1,
          "trainId": "dc-occ.eclrt-train-6250-guid",
          "arrivalTime": 3600,
          "departureTime": "15:01:00",
          "status": 0,
          "destination": "PL101",
          "serviceState": 1,
          "runNumber": 678
        }
      ]
    }
  ],
  "blockOccupancies": [],
  "gateCommands": []
}
```

---

## Wire Format — Encrypted Message to Kafka/RabbitMQ

```
byte[] payload sent to Kafka topic tms.scada.encrypted:

┌──────────────────┬──────────────────────────────────┬──────────────────┐
│   Bytes 0 – 11   │      Bytes 12 – (n-16)           │  Last 16 bytes   │
│                  │                                  │                  │
│   IV (12 bytes)  │   Encrypted JSON (ciphertext)    │  GCM Auth Tag    │
│   Random, fresh  │   AES-256-GCM encrypted          │  Tamper detect   │
│   per message    │                                  │                  │
└──────────────────┴──────────────────────────────────┴──────────────────┘

Algorithm : AES/GCM/NoPadding
Key size  : 256-bit (32 bytes)
Key source: ENV variable SCADA_AES_KEY (Base64 encoded)
            NEVER in config files or code
```

---

## Security Summary

| Layer | What | Where |
|---|---|---|
| **TLS — Artemis** | `ssl://` connection, JKS truststore | `SecurityConfig.java` + `application-{env}.properties` |
| **TLS — Kafka** | `SASL_SSL`, JKS truststore | `application-{env}.properties` |
| **TLS — RabbitMQ** | AMQPS port 5671, JKS truststore | `application-{env}.properties` |
| **XXE Prevention** | All external XML entity features disabled | `XmlToJsonProcessor` |
| **AES-256-GCM** | Authenticated encryption, fresh IV per message | `EncryptProcessor` |
| **Key from ENV only** | `SCADA_AES_KEY` — never in config/code | `EncryptProcessor` |
| **All broker values from ENV** | Host, port, user, password — all env vars | `application-{env}.properties` |
| **Dead Letter Queue** | Failed messages → `DLQ.kafka-bridge` | `KafkaBridgeRoutes` |
| **Retry with backoff** | 3 retries, 2s delay before dead-letter | `KafkaBridgeRoutes` |
| **No sensitive logging** | Topic name only — body never logged | All processors |

---

## Authentication — How Each Broker Checks Identity

```
OUR APP                          BROKER
────────                         ──────
sends username+password  ──►  Artemis checks artemis-users.properties
                               ✅ correct → subscribe allowed
                               ❌ wrong   → JMSSecurityException

sends SASL/PLAIN         ──►  Kafka checks user store + ACL
                               ✅ correct + ACL → produce/consume allowed
                               ❌ no ACL  → AuthorizationException

sends username+password  ──►  RabbitMQ checks internal user store
                               ✅ correct → publish to amq.topic allowed
                               ❌ wrong   → connection refused
```

---

## Environment Profiles

Spring Boot automatically picks the right config based on `SPRING_PROFILES_ACTIVE`.

```
SPRING_PROFILES_ACTIVE=prod
        │
        ▼
application.properties              ← always loaded (topics, camel — common to all)
        +
application-prod.properties         ← broker hosts/ports/TLS from env vars
```

| Profile | How to activate | TLS | Broker values |
|---|---|---|---|
| `local` | `mvn spring-boot:run -Dspring.profiles.active=local` | Off | Hardcoded localhost |
| `dev` | `export SPRING_PROFILES_ACTIVE=dev` | On | From env vars |
| `staging` | `export SPRING_PROFILES_ACTIVE=staging` | On | From env vars |
| `prod` | `export SPRING_PROFILES_ACTIVE=prod` | On | From env vars |

---

## Required Environment Variables (dev / staging / prod)

```bash
# Artemis
export ARTEMIS_HOST=10.12.1.13
export ARTEMIS_PORT=61617
export ARTEMIS_USER=pasbridge
export ARTEMIS_PASS=<password>

# Kafka
export KAFKA_HOST=10.12.1.14
export KAFKA_PORT=9093
export KAFKA_USER=tms_bridge
export KAFKA_PASS=<password>

# RabbitMQ
export RABBITMQ_HOST=10.12.1.11
export RABBITMQ_PORT=5671
export RABBITMQ_USER=tms_bridge
export RABBITMQ_PASS=<password>

# TLS — single truststore covers all 3 brokers
export TLS_TRUSTSTORE_PATH=/opt/pinkline/certs/truststore.jks
export TLS_TRUSTSTORE_PASS=<password>

# Encryption
export SCADA_AES_KEY=<base64-256bit-key>

# Profile
export SPRING_PROFILES_ACTIVE=prod
```

If any variable is missing, the app **fails immediately** at startup with a clear error — nothing runs silently with wrong config.

---

## How TLS Works

```
App ──(TLS ssl://)──► Artemis    port 61617
App ──(SASL_SSL)──►  Kafka       port 9093
App ──(AMQPS)────►   RabbitMQ    port 5671
```

A single JKS truststore file contains CA certificates for all 3 brokers.
`SecurityConfig.java` wires the truststore into the Artemis SSL factory.
Kafka and RabbitMQ read TLS config directly from `application-{env}.properties`.

Local profile uses plain TCP/AMQP — no truststore needed for Docker testing.

---

## Project Files

```
PAS-SCADA-Kafka-Bridge/
├── pom.xml                                         Maven dependencies
├── docker-compose.yml                              Local test (Artemis+Kafka+RabbitMQ)
├── ARCHITECTURE.md                                 This file
│
└── src/
    ├── main/
    │   ├── java/com/pinkline/kafkabridge/
    │   │   ├── KafkaBridgeApplication.java         Spring Boot entry point
    │   │   ├── config/
    │   │   │   └── SecurityConfig.java             TLS + auth config for all brokers
    │   │   ├── model/
    │   │   │   ├── ATRTimeTable.java               XML POJO — timetable
    │   │   │   ├── SingleArrival.java              XML POJO — train arrived
    │   │   │   ├── SingleDeparture.java            XML POJO — train departed
    │   │   │   └── RouteInfo.java                  XML POJO — route info
    │   │   ├── processor/
    │   │   │   ├── XmlToJsonProcessor.java         Jackson XML→ICD JSON
    │   │   │   └── EncryptProcessor.java           AES-256-GCM encryption
    │   │   └── routes/
    │   │       └── KafkaBridgeRoutes.java          Camel routes — all topics from config
    │   └── resources/
    │       ├── application.properties              Common config (topics, camel)
    │       ├── application-local.properties        Local Docker — no TLS, hardcoded
    │       ├── application-dev.properties          Dev server — TLS on, env vars
    │       ├── application-staging.properties      Staging server — TLS on, env vars
    │       └── application-prod.properties         Production — TLS on, env vars
    │
    └── test/
        └── java/com/pinkline/kafkabridge/
            ├── XmlToJsonProcessorTest.java         Tests all 4 XML formats
            ├── EncryptProcessorTest.java           Tests encryption/decryption
            └── TestXmlPublisher.java               Simulates TMS publishing XML
```

---

## Quick Start — Local Test

```bash
# 1. Start all servers locally (Docker)
docker-compose up -d

# 2. Set ENV variables
export SCADA_AES_KEY=$(openssl rand -base64 32)
export ARTEMIS_PASS=testpass123
export RABBITMQ_PASS=testpass123
export KAFKA_PASS=testpass123

# 3. Run unit tests (no Docker needed)
mvn test

# 4. Start the bridge (local profile)
mvn spring-boot:run -Dspring-boot.run.profiles=local

# 5. Simulate TMS sending XML
mvn exec:java -Dexec.mainClass="com.pinkline.kafkabridge.TestXmlPublisher"

# 6. Check results
#    Artemis:  http://localhost:8161
#    RabbitMQ: http://localhost:15672
```

---

## Before Going to Production — Admin Checklist

```
□ Kafka installed at 10.12.1.14:9092                        (infra team)
□ Kafka topic created: tms.scada.encrypted                  (infra team)
□ Kafka user tms_bridge created + ACLs                      (infra team)
□ Artemis user pasbridge created                            (server admin)
□ Artemis subscribe permissions on 4 topics                 (server admin)
□ RabbitMQ user tms_bridge with write on amq.topic          (server admin)
□ RabbitMQ MQTT plugin enabled                              (server admin)
□ AES key generated: openssl rand -base64 32                (Thiru)
□ AES key shared securely with SCADA team                   (Thiru + SCADA team)
□ SCADA team confirms MQTT receive + decrypt works          (SCADA team)
□ Bala sign-off                                             (Bala)
```
