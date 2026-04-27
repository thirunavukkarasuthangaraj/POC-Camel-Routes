# Custom Kafka Connect image with the full set of Camel connectors needed
# by the PAS-SCADA pipeline. Built once, deployed many times. No runtime
# downloads.
#
# Plugins installed:
#   1. camel-jms-pooled-apache-artemis-source  (Phase 2 — TMS Artemis → Kafka)
#   2. camel-spring-rabbitmq-sink              (Phase 4 — Kafka → RabbitMQ AMQP)
#   3. camel-mqtt5-sink                        (Phase 4 — Kafka → MQTT)
#   4. camel-spring-rabbitmq-source            (Phase 4 — RabbitMQ → Kafka, reverse)
#   5. camel-jms-pooled-apache-artemis-sink    (Phase 4 — Kafka → Artemis, reverse)

FROM confluentinc/cp-kafka-connect:7.5.0

ARG CKC_VERSION=4.8.3
ARG MAVEN_BASE=https://repo1.maven.org/maven2/org/apache/camel/kafkaconnector

USER root

# Install all five plugins in a single RUN to keep layer count down.
# Each tarball includes its full dependency tree (Camel core, JMS client,
# pooled-jms, RabbitMQ client, Paho MQTT, etc.).
#
# --exclude=docs strips the docs/ sibling directory inside each tarball
# (otherwise the Connect plugin scanner logs a benign "no plugin found"
# warning at startup for every tarball that includes it).
RUN set -eux; \
    PLUGINS="camel-jms-pooled-apache-artemis-source-kafka-connector \
             camel-spring-rabbitmq-sink-kafka-connector \
             camel-mqtt5-sink-kafka-connector \
             camel-spring-rabbitmq-source-kafka-connector \
             camel-jms-pooled-apache-artemis-sink-kafka-connector"; \
    mkdir -p /usr/share/confluent-hub-components; \
    for p in $PLUGINS; do \
      url="${MAVEN_BASE}/${p}/${CKC_VERSION}/${p}-${CKC_VERSION}-package.tar.gz"; \
      echo "→ ${p}"; \
      curl -fsSL -o /tmp/plugin.tar.gz "${url}"; \
      tar -xzf /tmp/plugin.tar.gz -C /usr/share/confluent-hub-components/ \
          --exclude='docs'; \
      rm /tmp/plugin.tar.gz; \
    done; \
    ls /usr/share/confluent-hub-components/

USER appuser

EXPOSE 8083
