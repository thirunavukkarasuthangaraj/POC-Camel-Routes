# How to Start PAS-SCADA Kafka Bridge

---

## What is this app?

This app sits between the **TMS server** (train management) and **SCADA** (station displays).

```
TMS Server                    Your Bridge App               SCADA
──────────                    ───────────────               ─────
Publishes XML  ──► Artemis ──► reads XML
                              converts to JSON
                              encrypts (AES-256)
                              ──► Kafka ──► reads encrypted
                                           decrypts
                                           ──► RabbitMQ ──► subscribes MQTT
```

---

## Batch Files (Quick Reference)

| File | What it does |
|------|-------------|
| `start-local.bat` | Start Docker + start app (LOCAL test) |
| `stop-local.bat` | Stop everything |
| `send-test-xml.bat` | Simulate TMS — send test XML to Artemis |
| `run-tests.bat` | Run unit tests (no Docker needed) |
| `start-production.bat` | Start app for REAL servers |

---

## STEP 1 — Install Requirements (one time only)

### 1a. Install Java 17
- Download: https://adoptium.net
- After install, open CMD and type: `java -version`
- You should see: `openjdk version "17..."`

### 1b. Install Maven 3.8+
- Download: https://maven.apache.org/download.cgi
- Extract to `C:\maven`
- Add `C:\maven\bin` to Windows PATH
- Open CMD and type: `mvn -version`
- You should see: `Apache Maven 3.x.x`

### 1c. Install Docker Desktop
- Download: https://www.docker.com/products/docker-desktop
- After install, start Docker Desktop
- Wait until Docker icon in taskbar shows "Running"

---

## STEP 2 — Run Unit Tests First (verify code is OK)

Double-click: **`run-tests.bat`**

What it does:
- No Docker, no servers needed
- Tests XML → JSON conversion (5 tests)
- Tests AES-256 encryption (4 tests)
- All 9 must PASS before moving to next step

Expected output:
```
Tests run: 4, Failures: 0, Errors: 0  (EncryptProcessorTest)
Tests run: 5, Failures: 0, Errors: 0  (XmlToJsonProcessorTest)
Tests run: 9, Failures: 0, Errors: 0
BUILD SUCCESS
ALL TESTS PASSED
```

If tests FAIL — stop here, fix the error first.

> **First run note:** Maven will download all dependencies (~100 MB).
> This takes a few minutes on first run only. Subsequent runs are fast.

---

## STEP 3 — Start Local Test Environment

Double-click: **`start-local.bat`**

What it does (automatically):
1. Checks Java is installed
2. Checks Maven is installed
3. Checks Docker is running
4. Runs `docker-compose up -d` — starts 4 containers:
   - **Artemis** on port 61616 (simulates real TMS broker)
   - **Kafka** on port 9092 (message store)
   - **Zookeeper** on port 2181 (Kafka dependency)
   - **RabbitMQ** on port 5672 (for SCADA)
5. Waits 15 seconds for containers to be ready
6. Starts the Spring Boot bridge app with `local` profile

> **First run note:** Docker will download images (~1.5 GB total).
> This takes several minutes on first run only.

App is running when you see:
```
Started KafkaBridgeApplication in X seconds
```

Keep this window OPEN — it's the running app.

---

## STEP 4 — Send Test XML (Simulate TMS)

Open a NEW CMD window.
Double-click: **`send-test-xml.bat`**

What it does:
- Connects to local Docker Artemis
- Sends 4 real XML messages (same format as TMS server):
  - ATRTimeTable → topic `TMS.PISInfo`
  - SingleArrival → topic `RCS.E2K.TMS.TrafficReportClient`
  - SingleDeparture → topic `RCS.E2K.TMS.TrafficReportClient`
  - RouteInfo → topic `RCS.E2K.TMS.RouteInfo`

Expected output:
```
Sent to topic: TMS.PISInfo [schema: RCS.E2K.TMS.ATRTimeTableMsg.V3]
Sent to topic: RCS.E2K.TMS.TrafficReportClient [schema: ...SingleArrival.V2]
Sent to topic: RCS.E2K.TMS.TrafficReportClient [schema: ...SingleDeparture.V2]
Sent to topic: RCS.E2K.TMS.RouteInfo [schema: RCS.E2K.TMS.RouteInfo.V2]
Published 4 real TMS XML messages.
```

---

## STEP 5 — Verify It Worked

### Check Bridge App Logs (in start-local.bat window)
You should see:
```
Received message from Artemis topic: TMS.PISInfo
XML converted to JSON successfully
Published encrypted message to Kafka — topic: tms.scada.encrypted
Received encrypted message from Kafka, forwarding to RabbitMQ
Forwarded to RabbitMQ — routing key: tms.scada.pas
```

### Check Kafka received messages
Open CMD and run:
```cmd
docker exec -it test-kafka kafka-console-consumer ^
  --bootstrap-server localhost:9092 ^
  --topic tms.scada.encrypted ^
  --from-beginning
```
You will see encrypted binary output — that means Kafka received the messages.

### Check RabbitMQ Management UI
Open browser: http://localhost:15672
- Username: `guest`
- Password: `guest`
- Go to: **Queues** tab
- Look for queue: `tms.scada.pas`
- Should show messages received

### Check Artemis Management UI
Open browser: http://localhost:8161
- Username: `pasbridge`
- Password: `testpass123`
- Go to: **Addresses** tab
- Should see the 4 topics with message counts

---

## STEP 6 — Stop Everything

Double-click: **`stop-local.bat`**

What it does:
- Stops all Docker containers (Artemis, Kafka, Zookeeper, RabbitMQ)
- Kills any Java process still running on port 8085

---

## PRODUCTION Start (Real Servers)

### Step 1 — Set environment variables

All broker details and secrets must be set as environment variables before starting.
Nothing is hardcoded — the app reads everything from the environment.

**Linux/Mac (production server):**
```bash
# Profile
export SPRING_PROFILES_ACTIVE=prod

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

# TLS (single truststore for all 3 brokers)
export TLS_TRUSTSTORE_PATH=/opt/pinkline/certs/truststore.jks
export TLS_TRUSTSTORE_PASS=<password>

# Encryption key
export SCADA_AES_KEY=<base64-256bit-key>
```

**Windows (CMD as Administrator):**
```cmd
set SPRING_PROFILES_ACTIVE=prod
set ARTEMIS_HOST=10.12.1.13
set ARTEMIS_PORT=61617
set ARTEMIS_USER=pasbridge
set ARTEMIS_PASS=<password>
set KAFKA_HOST=10.12.1.14
set KAFKA_PORT=9093
set KAFKA_USER=tms_bridge
set KAFKA_PASS=<password>
set RABBITMQ_HOST=10.12.1.11
set RABBITMQ_PORT=5671
set RABBITMQ_USER=tms_bridge
set RABBITMQ_PASS=<password>
set TLS_TRUSTSTORE_PATH=C:\pinkline\certs\truststore.jks
set TLS_TRUSTSTORE_PASS=<password>
set SCADA_AES_KEY=<base64-256bit-key>
```

### Step 2 — Generate AES key (one time only)

```bash
# Linux/Mac:
openssl rand -base64 32

# Windows PowerShell:
[Convert]::ToBase64String((1..32 | % { [byte](Get-Random -Max 256) }))
```

Share the same key securely with the SCADA team — they need it to decrypt.

### Step 3 — Start the app

```bash
java -jar PAS-SCADA-Kafka-Bridge.jar
```

Or double-click: **`start-production.bat`**

---

## Environment Profiles

The app automatically switches config based on `SPRING_PROFILES_ACTIVE`.

| Profile | Use for | TLS | Broker addresses |
|---|---|---|---|
| `local` | Local Docker testing | Off | `localhost` hardcoded |
| `dev` | Dev server | On | From env vars |
| `staging` | Staging server | On | From env vars |
| `prod` | Production | On | From env vars |

Same code, same JAR — only the environment variables change per server.

---

## Security, Encryption & Authentication

### Overview — 3 Layers of Security

```
Layer 1 — Authentication    : Who can connect to each broker
Layer 2 — Encryption        : Data encrypted before going to Kafka
Layer 3 — Message Integrity : GCM tag detects if anyone tampered the data
```

---

### Layer 1 — Authentication (who can connect)

#### Artemis (TMS broker)
```
Our app user : pasbridge
Password     : from ENV variable ARTEMIS_PASS
How it works : Artemis checks artemis-users.properties file
               Wrong password → JMSSecurityException → app refuses to start
Admin must do: Create user pasbridge on Artemis server
               Grant subscribe on topics: TMS.PISInfo, RCS.E2K.TMS.TrafficReportClient,
               TSInfo, RCS.E2K.TMS.RouteInfo
```

#### Kafka
```
Our app user : tms_bridge
Password     : from ENV variable KAFKA_PASS
Protocol     : SASL_PLAINTEXT (internal network) or SASL_SSL (with TLS)
How it works : Kafka SASL checks username/password
               Then checks ACLs before allowing read/write
               Wrong credentials → AuthenticationException
               No ACL → AuthorizationException
Admin must do: Create user tms_bridge on Kafka
               kafka-acls --add --allow-principal User:tms_bridge --operation Write --topic tms.scada.encrypted
               kafka-acls --add --allow-principal User:tms_bridge --operation Read  --topic tms.scada.encrypted
```

#### RabbitMQ
```
Our app user : tms_bridge (production) / guest (local Docker test)
Password     : from ENV variable RABBITMQ_PASS
How it works : Spring AMQP connects with username/password
               Wrong credentials → connection refused
Admin must do: Create user tms_bridge on RabbitMQ
               Set permissions: write=amq.topic on vhost /
               Enable MQTT: rabbitmq-plugins enable rabbitmq_mqtt
```

---

### Layer 2 — Encryption (AES-256-GCM)

**What is encrypted:** JSON string after XML→JSON conversion
**Algorithm:** AES-256-GCM (industry standard, same as TLS 1.3)
**Key size:** 256-bit (32 bytes)

```
Key source:  ENV variable SCADA_AES_KEY (Base64 encoded)
             NEVER stored in config files or code
             Must be the SAME key on Bridge server AND SCADA server

Wire format: [12 bytes IV][encrypted JSON bytes][16 bytes GCM tag]
              └──random──┘└────────────────────────────────────────┘
              Fresh IV every message (prevents pattern analysis)
```

**Generate the key (one time, admin does this):**
```cmd
REM Windows PowerShell:
[Convert]::ToBase64String((1..32 | % { [byte](Get-Random -Max 256) }))

REM Linux/Mac:
openssl rand -base64 32
```

**Set the key on Bridge server:**
```cmd
set SCADA_AES_KEY=<the-generated-base64-key>
```

**Set the SAME key on SCADA server:**
```cmd
export SCADA_AES_KEY=<the-same-generated-base64-key>
```

---

### Layer 3 — Message Integrity (GCM Authentication Tag)

AES-GCM automatically adds a 16-byte authentication tag to every message.

```
When SCADA decrypts:
  - Tag is verified automatically
  - If message was tampered in transit → AEADBadTagException
  - SCADA must treat this as a SECURITY ALERT and discard the message
  - This means: man-in-the-middle attacks are detected automatically
```

---

### How SCADA Decrypts (SCADA team reference)

Code is in: `src/main/java/com/pinkline/kafkabridge/processor/DecryptExample.java`

```java
// SCADA side — Java example
byte[] payload = // raw bytes from Kafka topic or MQTT

// Wire format: [12B IV][ciphertext+GCM tag]
byte[] iv = Arrays.copyOfRange(payload, 0, 12);
byte[] ciphertext = Arrays.copyOfRange(payload, 12, payload.length);

byte[] keyBytes = Base64.getDecoder().decode(System.getenv("SCADA_AES_KEY"));
SecretKey key = new SecretKeySpec(keyBytes, "AES");

Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(128, iv));

String json = new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
// json = {"messageType":"TMS_PAS_UPDATE","platformPredictions":[...]}
```

---

### Security Checklist (Admin must do before production)

#### Bridge Server
- [ ] Set ENV: `SPRING_PROFILES_ACTIVE=prod`
- [ ] Set ENV: `ARTEMIS_HOST`, `ARTEMIS_PORT`, `ARTEMIS_USER`, `ARTEMIS_PASS`
- [ ] Set ENV: `KAFKA_HOST`, `KAFKA_PORT`, `KAFKA_USER`, `KAFKA_PASS`
- [ ] Set ENV: `RABBITMQ_HOST`, `RABBITMQ_PORT`, `RABBITMQ_USER`, `RABBITMQ_PASS`
- [ ] Set ENV: `TLS_TRUSTSTORE_PATH`, `TLS_TRUSTSTORE_PASS`
- [ ] Set ENV: `SCADA_AES_KEY=<base64-32-bytes>`
- [ ] Place JKS truststore at `TLS_TRUSTSTORE_PATH` (contains CA certs for all 3 brokers)

#### Artemis Server (10.12.1.13)
- [ ] Create user: `pasbridge` with password
- [ ] Grant subscribe on 4 TMS topics

#### Kafka Server (10.12.1.14)
- [ ] Create user: `tms_bridge`
- [ ] Create topic: `tms.scada.encrypted` (retention 24h)
- [ ] Add ACL: `tms_bridge` WRITE on `tms.scada.encrypted`
- [ ] Add ACL: `tms_bridge` READ on `tms.scada.encrypted` (group: scada-bridge-consumer)

#### RabbitMQ Server (10.12.1.11)
- [ ] Create user: `tms_bridge` with password
- [ ] Set permissions: write=`amq.topic` on vhost `/`
- [ ] Enable MQTT plugin: `rabbitmq-plugins enable rabbitmq_mqtt`
- [ ] Enable MQTT TLS on port 8883 (for SCADA)

#### SCADA Server
- [ ] Set SAME ENV: `SCADA_AES_KEY=<same-key-as-bridge-server>`
- [ ] Implement decryption using `DecryptExample.java` as reference

---

### Security Summary Table

| What | How | Where configured |
|------|-----|-----------------|
| Artemis password | ENV: `ARTEMIS_PASS` | `SecurityConfig.java` |
| Kafka password | ENV: `KAFKA_PASS` | `application.properties` SASL config |
| RabbitMQ password | ENV: `RABBITMQ_PASS` | `application.properties` spring.rabbitmq |
| AES-256 encryption key | ENV: `SCADA_AES_KEY` | `EncryptProcessor.java` |
| Message integrity | AES-GCM tag (automatic) | `EncryptProcessor.java` |
| Failed message recovery | Dead Letter Queue | `KafkaBridgeRoutes.java` |
| XXE attack prevention | Jackson XmlMapper (safe by default) | `XmlToJsonProcessor.java` |

---

## Troubleshooting

### "Docker not running"
- Open Docker Desktop
- Wait until taskbar icon says "Running"
- Try again

### "Port 61616 already in use"
- Another Artemis or ActiveMQ is running
- Run: `stop-local.bat` then try again

### "Connection refused to localhost:9092"
- Kafka container not ready yet
- Wait 30 seconds, try again

### "SCADA_AES_KEY not set"
- Production only: set the env variable first (see above)
- Local test: `start-local.bat` sets it automatically to a test key

### Tests FAIL with "NoSuchMethodError"
- Java version mismatch
- Run: `java -version` — must be 17 or higher

### Bridge app starts but no messages flowing
- Make sure you ran `send-test-xml.bat`
- In LOCAL mode — Artemis is empty at start, `send-test-xml.bat` fills it
- In PRODUCTION — TMS server fills it automatically, nothing extra needed

### First `mvn test` is slow
- Normal — Maven downloads all dependencies on first run
- Second run onwards: fast (dependencies are cached locally)

### First `docker-compose up` is slow
- Normal — Docker downloads images (~1.5 GB) on first run
- Second run onwards: fast (images are cached locally)

---

## File Structure

```
PAS-SCADA-Kafka-Bridge/
│
├── start-local.bat          ← START for local testing
├── stop-local.bat           ← STOP everything
├── send-test-xml.bat        ← simulate TMS sending XML
├── run-tests.bat            ← 9 unit tests (no Docker)
├── start-production.bat     ← START for real servers
│
├── docker-compose.yml       ← Artemis + Kafka + Zookeeper + RabbitMQ
├── pom.xml                  ← Maven dependencies
├── README.md                ← single source of truth: architecture + deploy + cutover
├── HOWTO-START.md           ← this file
│
└── src/
    ├── main/
    │   ├── java/
    │   │   └── com/pinkline/kafkabridge/
    │   │       ├── KafkaBridgeApplication.java   ← app entry point
    │   │       ├── routes/
    │   │       │   └── KafkaBridgeRoutes.java    ← Camel routes (wiring)
    │   │       ├── processor/
    │   │       │   ├── XmlToJsonProcessor.java   ← XML → JSON (Jackson)
    │   │       │   └── EncryptProcessor.java     ← AES-256-GCM encrypt
    │   │       ├── model/
    │   │       │   ├── ATRTimeTable.java         ← XML POJO (timetable)
    │   │       │   ├── SingleArrival.java        ← XML POJO (arrival)
    │   │       │   ├── SingleDeparture.java      ← XML POJO (departure)
    │   │       │   └── RouteInfo.java            ← XML POJO (route)
    │   │       └── config/
    │   │           └── SecurityConfig.java       ← auth config
    │   └── resources/
    │       ├── application.properties            ← common (topics, camel)
    │       ├── application-local.properties      ← local Docker, no TLS
    │       ├── application-dev.properties        ← dev server, TLS on, env vars
    │       ├── application-staging.properties    ← staging server, TLS on, env vars
    │       └── application-prod.properties       ← production, TLS on, env vars
    │
    └── test/
        └── java/com/pinkline/kafkabridge/
            ├── XmlToJsonProcessorTest.java       ← 5 XML conversion tests
            ├── EncryptProcessorTest.java         ← 4 encryption tests
            └── TestXmlPublisher.java             ← simulate TMS (not JUnit)
```

---

## Summary: Normal Daily Flow

```
Day 1 (one time):
  Install Java 17 + Maven + Docker Desktop

Every test session:
  1. Double-click run-tests.bat          ← verify 9 tests pass
  2. Double-click start-local.bat        ← starts Docker + app
  3. Wait for "Started KafkaBridgeApplication"
  4. Double-click send-test-xml.bat      ← simulate TMS
  5. Watch logs in start-local.bat window
  6. Double-click stop-local.bat         ← stop when done

Production deployment:
  1. Set SCADA_AES_KEY, ARTEMIS_PASS, RABBITMQ_PASS
  2. Double-click start-production.bat
```

---

## Test Results (confirmed working)

```
EncryptProcessorTest    : 4 tests PASS  (AES-256-GCM encryption)
XmlToJsonProcessorTest  : 5 tests PASS  (XML to ICD JSON conversion)
─────────────────────────────────────────────────────────────────
Total                   : 9 tests PASS, 0 failures
BUILD SUCCESS
```
