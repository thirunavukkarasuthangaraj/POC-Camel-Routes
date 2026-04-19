# Cairo App — Artemis Connection Details

Fill in the values below from the Cairo app's Artemis configuration,
then give this file back so the bridge config can be updated.

---

## Connection

| Property       | Value |
|----------------|-------|
| Host IP        |       |
| Port           | 61616 |
| Username       |       |
| Password       |       |

---

## Topics (published by Cairo app)

List every Artemis topic the Cairo app publishes to.
The bridge will subscribe to all of them.

| # | Topic Name |
|---|------------|
| 1 |            |
| 2 |            |
| 3 |            |
| 4 |            |

---

## Where to find these in the Cairo app

- **Host / Port** → `broker.xml` or `application.properties` under `activemq.broker-url`
- **Username / Password** → Artemis user config or environment variables `ARTEMIS_USER` / `ARTEMIS_PASS`
- **Topic names** → Camel route `from("activemq:topic:TOPIC_NAME")` or `application.properties`

---

## What will be updated once filled

| File | Change |
|------|--------|
| `tms/docker-compose.yml` | `ARTEMIS_HOST`, `ARTEMIS_PORT`, `ARTEMIS_USER`, `ARTEMIS_PASS` |
| `tms/src/main/resources/application.properties` | `bridge.artemis-topics=TOPIC1,TOPIC2,...` |
