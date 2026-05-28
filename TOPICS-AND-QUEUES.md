# Topics & Queues — Full Inventory (verify checklist)

Every queue, topic, routing key, and address in the TMS ↔ SCADA pipeline,
with the command to verify it. Use this to check the Azure cluster.

Directions: **F** = TMS → SCADA (forward), **R** = SCADA → TMS (reverse).

---

## 1. RabbitMQ (namespace `scada`)

Exchange used: **`amq.topic`** (type: topic).

### Queues
| Queue | Dir | Purpose |
|---|---|---|
| `mqtt-subscription-scada-sim-ScateXqos0` | F | SCADA's MQTT subscription (auto-created) |
| `scada.monitor.queue` | — | Monitoring |
| **`scada.tms.alarms.queue`** | R | Catches SCADA→TMS alarms for the reverse path |

### Bindings on `amq.topic`
| Routing key | → Queue | Dir |
|---|---|---|
| `tms.scada.pas` | `mqtt-subscription-scada-sim-ScateXqos0` | F |
| `tms.scada.pas` | `scada.monitor.queue` | — |
| `scada.tms.#` (or `scada.tms.alarms`) | `scada.tms.alarms.queue` | R |

### Verify (in the rabbitmq pod)
```bash
POD=$(kubectl -n scada get pods -l app=rabbitmq -o jsonpath='{.items[0].metadata.name}')
kubectl -n scada exec $POD -- rabbitmqctl list_queues name messages
kubectl -n scada exec $POD -- rabbitmqctl list_bindings source_name routing_key destination_name
```

---

## 2. MQTT topics (SCADA-facing, via RabbitMQ MQTT plugin)

| Topic | Dir | Payload |
|---|---|---|
| `tms/scada/pas` | F | Encrypted (AES-256-GCM) |
| `scada/tms/alarms` | R | Plain JSON (RSAE envelope) |

> MQTT `/` becomes `.` as the AMQP routing key (`scada/tms/alarms` → `scada.tms.alarms`).

---

## 3. Kafka topics (namespace `kafka` / `pinkline`)

| Topic | Dir | Notes |
|---|---|---|
| `tms.raw` | F | Raw XML from Artemis |
| `tms.scada.encrypted` | F | AES-256-GCM, before RabbitMQ |
| `scada.tms.raw` | R | From RabbitMQ queue |
| `scada.tms.processed` | R | After bridge transform, before Artemis |
| `scada.tms.alarms.state` | R | Compacted state topic (optional) |

DLQ topics (only in the Kafka-Connect / Strimzi setup — Azure):
`dlq.connect.tms-artemis-source`, `dlq.connect.tms-rabbitmq-sink`,
`dlq.connect.scada-rabbitmq-source`, `dlq.connect.scada-artemis-sink`.

### Verify
```bash
# Azure (Strimzi, namespace kafka)
kubectl -n kafka exec kafka-cluster-kafka-pool-0 -- \
  bin/kafka-topics.sh --bootstrap-server localhost:9092 --list

# message count for a topic
kubectl -n kafka exec kafka-cluster-kafka-pool-0 -- \
  bin/kafka-get-offsets.sh --bootstrap-server localhost:9092 --topic scada.tms.processed
```

---

## 4. Artemis addresses

| Address | Dir |
|---|---|
| `TMS.PISInfo` | F |
| `RCS.E2K.TMS.TrafficReportClient` | F |
| `TSInfo` | F |
| `RCS.E2K.TMS.RouteInfo` | F |
| `SCADA.TMS.Alarms` | R |

### Verify
```bash
kubectl -n artemis exec artemis-0 -- \
  /var/lib/artemis-instance/bin/artemis queue stat \
  --user "$ARTEMIS_USER" --password "$ARTEMIS_PASS" --url tcp://localhost:61616 \
  | grep -E "SCADA.TMS.Alarms|TMS.PISInfo"
```

---

## 5. Reverse-path consumer groups (Azure / Kafka Connect)

| Group | Reads topic | Role |
|---|---|---|
| `pas-bridge-consumer` | `tms.scada.encrypted` | Forward |
| `pas-bridge-reverse` | `scada.tms.raw` | Reverse transform → `scada.tms.processed` |
| `pas-bridge-artemis-sink` | `scada.tms.processed` | Reverse sink → Artemis |

```bash
kubectl -n kafka exec kafka-cluster-kafka-pool-0 -- \
  bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups
```

---

## Known fixes that must be in place (Azure)

These are the two issues found while debugging SCADA→TMS:

1. **RabbitMQ reverse queue + binding** — `scada.tms.alarms.queue` bound to
   `amq.topic` key `scada.tms.#`. (Without it, SCADA→TMS messages are
   dropped at the exchange.)
2. **Bridge reverse decrypt off** — `BRIDGE_REVERSE_KAFKA_ENCRYPT_ENABLED=false`.
   SCADA→TMS is plain JSON; with decrypt on, the bridge throws
   `AEADBadTagException` and dead-letters every message.
3. **Bridge → Artemis host** — `ARTEMIS_HOST` must point at the in-cluster
   service (`artemis.artemis.svc.cluster.local`), not the external IP
   `10.4.1.28` (which refuses the connection).

---
_Generated 2026-05-26 from the deployed configs + live verification._
