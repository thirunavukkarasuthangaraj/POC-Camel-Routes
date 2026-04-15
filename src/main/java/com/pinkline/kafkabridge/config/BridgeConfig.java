package com.pinkline.kafkabridge.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;

/**
 * BridgeConfig
 *
 * Binds all bridge.* properties into typed Java objects.
 *
 * Outbound (Artemis → Kafka):
 *   bridge.artemis.topics = comma-separated list of Artemis topics
 *   bridge.kafka.topic    = Kafka output topic
 *
 * Forward (Kafka → RabbitMQ):
 *   bridge.forward[0].from-kafka     = Kafka topic to consume
 *   bridge.forward[0].to-exchange    = RabbitMQ exchange
 *   bridge.forward[0].routing-key    = RabbitMQ routing key
 *   bridge.forward[0].consumer-group = Kafka consumer group ID
 *
 * Inbound (RabbitMQ → Artemis):
 *   bridge.inbound[0].from-exchange  = RabbitMQ exchange to consume
 *   bridge.inbound[0].routing-key    = RabbitMQ routing key
 *   bridge.inbound[0].to-topic       = Artemis topic to publish to
 *
 * Adding a new route = adding a new entry in application.properties.
 * Zero code changes needed.
 */
@Component
@ConfigurationProperties(prefix = "bridge")
public class BridgeConfig {

    // ── Outbound: Artemis → Kafka ─────────────────────────────────
    private String artemisTopics;
    private Kafka kafka = new Kafka();

    // ── Forward: Kafka → RabbitMQ (one entry per route) ──────────
    private List<ForwardRoute> forward = new ArrayList<>();

    // ── Inbound: RabbitMQ → Artemis (one entry per route) ────────
    private List<InboundRoute> inbound = new ArrayList<>();

    // ── Getters / Setters ─────────────────────────────────────────

    public String getArtemisTopics() { return artemisTopics; }
    public void setArtemisTopics(String artemisTopics) { this.artemisTopics = artemisTopics; }

    public Kafka getKafka() { return kafka; }
    public void setKafka(Kafka kafka) { this.kafka = kafka; }

    public List<ForwardRoute> getForward() { return forward; }
    public void setForward(List<ForwardRoute> forward) { this.forward = forward; }

    public List<InboundRoute> getInbound() { return inbound; }
    public void setInbound(List<InboundRoute> inbound) { this.inbound = inbound; }

    // ── Inner: Kafka config ───────────────────────────────────────
    public static class Kafka {
        private String topic;
        private String consumerGroup;

        public String getTopic() { return topic; }
        public void setTopic(String topic) { this.topic = topic; }

        public String getConsumerGroup() { return consumerGroup; }
        public void setConsumerGroup(String consumerGroup) { this.consumerGroup = consumerGroup; }
    }

    // ── Inner: Kafka → RabbitMQ route ────────────────────────────
    public static class ForwardRoute {
        private String fromKafka;
        private String toExchange;
        private String routingKey;
        private String consumerGroup;

        public String getFromKafka() { return fromKafka; }
        public void setFromKafka(String fromKafka) { this.fromKafka = fromKafka; }

        public String getToExchange() { return toExchange; }
        public void setToExchange(String toExchange) { this.toExchange = toExchange; }

        public String getRoutingKey() { return routingKey; }
        public void setRoutingKey(String routingKey) { this.routingKey = routingKey; }

        public String getConsumerGroup() { return consumerGroup; }
        public void setConsumerGroup(String consumerGroup) { this.consumerGroup = consumerGroup; }
    }

    // ── Inner: RabbitMQ → Artemis route ──────────────────────────
    public static class InboundRoute {
        private String fromExchange;
        private String routingKey;
        private String toTopic;

        public String getFromExchange() { return fromExchange; }
        public void setFromExchange(String fromExchange) { this.fromExchange = fromExchange; }

        public String getRoutingKey() { return routingKey; }
        public void setRoutingKey(String routingKey) { this.routingKey = routingKey; }

        public String getToTopic() { return toTopic; }
        public void setToTopic(String toTopic) { this.toTopic = toTopic; }
    }
}
