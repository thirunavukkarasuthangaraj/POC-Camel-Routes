package com.pinkline.kafkabridge.routes;

import com.pinkline.kafkabridge.config.BridgeConfig;
import com.pinkline.kafkabridge.processor.EncryptProcessor;
import com.pinkline.kafkabridge.processor.XmlToJsonProcessor;
import org.apache.camel.LoggingLevel;
import org.apache.camel.builder.RouteBuilder;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

/**
 * KafkaBridgeRoutes — Generic Camel Router
 *
 * All routes are built dynamically from application.properties.
 * Adding a new route = adding config only. Zero code changes.
 *
 * ── Route Type 1: Artemis → Kafka (Outbound) ─────────────────────
 *   Reads bridge.artemis.topics (comma-separated)
 *   One Camel route created per topic automatically.
 *   Flow: from(artemis:topic) → XmlToJson → Encrypt → to(kafka)
 *
 * ── Route Type 2: Kafka → RabbitMQ (Forward) ─────────────────────
 *   Reads bridge.forward[n] entries
 *   One Camel route created per entry automatically.
 *   Flow: from(kafka:topic) → to(spring-rabbitmq:exchange)
 *
 * ── Route Type 3: RabbitMQ → Artemis (Inbound) ───────────────────
 *   Reads bridge.inbound[n] entries
 *   One Camel route created per entry automatically.
 *   Flow: from(spring-rabbitmq:exchange) → to(artemis:topic)
 *
 * Example — adding Power SCADA inbound (no code change needed):
 *   bridge.inbound[0].from-exchange=amq.topic
 *   bridge.inbound[0].routing-key=power.scada.tms
 *   bridge.inbound[0].to-topic=TMS.PowerScada.Info
 */
@Component
public class KafkaBridgeRoutes extends RouteBuilder {

    private static final String BYTE_SER   = "org.apache.kafka.common.serialization.ByteArraySerializer";
    private static final String BYTE_DESER = "org.apache.kafka.common.serialization.ByteArrayDeserializer";

    @Autowired
    private BridgeConfig config;

    @Override
    public void configure() {

        // ── Global error handler ──────────────────────────────────────────
        onException(Exception.class)
            .maximumRedeliveries(3)
            .redeliveryDelay(2000)
            .retryAttemptedLogLevel(LoggingLevel.WARN)
            .useOriginalMessage()
            .to("activemq:queue:DLQ.kafka-bridge")
            .log("Dead-letter queue — ${exception.message}")
            .handled(true);

        // ── Route Type 1: Artemis → Kafka (one route per topic) ──────────
        for (String topic : config.getArtemisTopics().split(",")) {
            topic = topic.trim();
            String clientId = "pas-bridge-" + topic.replace(".", "-").toLowerCase();
            String subName  = "pas-bridge-" + topic.replace(".", "-").toLowerCase();
            String routeId  = "outbound-" + topic.replace(".", "-").toLowerCase();

            from("activemq:topic:" + topic
                    + "?clientId=" + clientId
                    + "&subscriptionDurable=true"
                    + "&durableSubscriptionName=" + subName)
                .routeId(routeId)
                .log("Received from Artemis topic: " + topic)
                .process(new XmlToJsonProcessor())
                .process(new EncryptProcessor())
                .to("kafka:" + config.getKafka().getTopic()
                        + "?brokers={{kafka.brokers}}"
                        + "&serializerClass=" + BYTE_SER
                        + "&keySerializerClass=" + BYTE_SER)
                .log("Published to Kafka: " + config.getKafka().getTopic());
        }

        // ── Route Type 2: Kafka → RabbitMQ (one route per forward entry) ─
        for (BridgeConfig.ForwardRoute fwd : config.getForward()) {
            String routeId = "forward-" + fwd.getFromKafka().replace(".", "-")
                           + "-to-" + fwd.getRoutingKey().replace(".", "-");

            from("kafka:" + fwd.getFromKafka()
                    + "?brokers={{kafka.brokers}}"
                    + "&groupId=" + fwd.getConsumerGroup()
                    + "&autoOffsetReset=latest"
                    + "&deserializerClass=" + BYTE_DESER
                    + "&keyDeserializerClass=" + BYTE_DESER)
                .routeId(routeId)
                .log("Kafka → RabbitMQ | topic: " + fwd.getFromKafka()
                        + " → exchange: " + fwd.getToExchange()
                        + " | key: " + fwd.getRoutingKey())
                .to("spring-rabbitmq:" + fwd.getToExchange()
                        + "?routingKey=" + fwd.getRoutingKey()
                        + "&autoStartup=true")
                .log("Forwarded to RabbitMQ — " + fwd.getRoutingKey());
        }

        // ── Route Type 3: RabbitMQ → Artemis (one route per inbound entry) ─
        for (BridgeConfig.InboundRoute inb : config.getInbound()) {
            String routeId = "inbound-" + inb.getRoutingKey().replace(".", "-")
                           + "-to-" + inb.getToTopic().replace(".", "-");

            from("spring-rabbitmq:" + inb.getFromExchange()
                    + "?routingKey=" + inb.getRoutingKey()
                    + "&autoStartup=true")
                .routeId(routeId)
                .log("RabbitMQ → Artemis | key: " + inb.getRoutingKey()
                        + " → topic: " + inb.getToTopic())
                .to("activemq:topic:" + inb.getToTopic())
                .log("Forwarded to Artemis topic: " + inb.getToTopic());
        }
    }
}
