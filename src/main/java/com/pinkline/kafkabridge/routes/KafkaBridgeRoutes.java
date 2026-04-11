package com.pinkline.kafkabridge.routes;

import com.pinkline.kafkabridge.processor.EncryptProcessor;
import com.pinkline.kafkabridge.processor.XmlToJsonProcessor;
import org.apache.camel.LoggingLevel;
import org.apache.camel.builder.RouteBuilder;
import org.springframework.stereotype.Component;

/**
 * KafkaBridgeRoutes
 *
 * Route 1 — Artemis → Kafka
 *   Listens on 4 TMS topics, converts XML to JSON,
 *   encrypts with AES-256-GCM, publishes to Kafka.
 *
 * Route 2 — Kafka → RabbitMQ
 *   Reads encrypted messages from Kafka,
 *   forwards to RabbitMQ (MQTT → SCADA API).
 */
@Component
public class KafkaBridgeRoutes extends RouteBuilder {

    private static final String BYTE_SER   = "org.apache.kafka.common.serialization.ByteArraySerializer";
    private static final String BYTE_DESER = "org.apache.kafka.common.serialization.ByteArrayDeserializer";

    private static final String KAFKA_TOPIC = "tms.scada.encrypted";

    private static final String[] TMS_TOPICS = {
        "TMS.PISInfo",
        "RCS.E2K.TMS.TrafficReportClient",
        "TSInfo",
        "RCS.E2K.TMS.RouteInfo"
    };

    @Override
    public void configure() {

        onException(Exception.class)
            .maximumRedeliveries(3)
            .redeliveryDelay(2000)
            .retryAttemptedLogLevel(LoggingLevel.WARN)
            .useOriginalMessage()
            .to("activemq:queue:DLQ.kafka-bridge")
            .log("Message sent to dead-letter queue: ${exception.message}")
            .handled(true);

        // ── Route 1: Artemis → Kafka ──────────────────────────────────────
        for (String topic : TMS_TOPICS) {
            String clientId = "pas-bridge-" + topic.replace(".", "-").toLowerCase();
            String subName  = "pas-bridge-" + topic.replace(".", "-").toLowerCase();
            String routeId  = "route-" + topic.replace(".", "-").toLowerCase() + "-to-kafka";

            from("activemq:topic:" + topic
                    + "?clientId=" + clientId
                    + "&subscriptionDurable=true"
                    + "&durableSubscriptionName=" + subName)
                .routeId(routeId)
                .log("Received from Artemis: " + topic)
                .process(new XmlToJsonProcessor())
                .process(new EncryptProcessor())
                .to("kafka:" + KAFKA_TOPIC
                        + "?brokers={{kafka.brokers}}"
                        + "&serializerClass=" + BYTE_SER
                        + "&keySerializerClass=" + BYTE_SER)
                .log("Published to Kafka: " + KAFKA_TOPIC);
        }

        // ── Route 2: Kafka → RabbitMQ ─────────────────────────────────────
        from("kafka:" + KAFKA_TOPIC
                + "?brokers={{kafka.brokers}}"
                + "&groupId=pas-bridge-consumer"
                + "&autoOffsetReset=latest"
                + "&deserializerClass=" + BYTE_DESER
                + "&keyDeserializerClass=" + BYTE_DESER)
            .routeId("route-kafka-to-rabbitmq")
            .log("Forwarding from Kafka to RabbitMQ")
            .to("spring-rabbitmq:amq.topic"
                    + "?routingKey=tms.scada.pas"
                    + "&autoStartup=true")
            .log("Forwarded to RabbitMQ — tms.scada.pas");
    }
}
