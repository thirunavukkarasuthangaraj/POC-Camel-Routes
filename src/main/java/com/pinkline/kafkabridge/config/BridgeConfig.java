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
 * ── Pipeline (Artemis → ordered sinks) ────────────────────────────
 *   bridge.pipeline = comma-separated ordered list of sinks
 *   Supported steps: kafka, rabbitmq, mqtt
 *   Examples:
 *     bridge.pipeline=kafka,rabbitmq,mqtt   full chain
 *     bridge.pipeline=mqtt                  direct to MQTT (skip brokers)
 *     bridge.pipeline=kafka,mqtt            Kafka audit + direct MQTT
 *
 * ── Outbound: Artemis topics ──────────────────────────────────────
 *   bridge.artemis.topics = comma-separated list of Artemis topics
 *   bridge.kafka.topic    = Kafka output topic (used when kafka in pipeline)
 *
 * ── RabbitMQ sink (used when 'rabbitmq' is in pipeline) ──────────
 *   bridge.rabbitmq-out.exchange    = RabbitMQ exchange
 *   bridge.rabbitmq-out.routing-key = RabbitMQ routing key
 *
 * ── MQTT direct (used when 'mqtt' is in pipeline) ─────────────────
 *   bridge.mqtt.broker-url = tcp://host:1883
 *   bridge.mqtt.topic      = tms/scada/pas
 *   bridge.mqtt.qos        = 0|1|2 (default 1)
 *   bridge.mqtt.client-id  = unique client ID
 *   bridge.mqtt.username   = (optional)
 *   bridge.mqtt.password   = (optional)
 *
 * ── Relay routes: Kafka → RabbitMQ (optional, separate consumer) ─
 *   bridge.forward[0].from-kafka     = Kafka topic to consume
 *   bridge.forward[0].to-exchange    = RabbitMQ exchange
 *   bridge.forward[0].routing-key    = RabbitMQ routing key
 *   bridge.forward[0].consumer-group = Kafka consumer group ID
 *
 * ── Inbound: RabbitMQ → Artemis ───────────────────────────────────
 *   bridge.inbound[0].from-exchange  = RabbitMQ exchange to consume
 *   bridge.inbound[0].routing-key    = RabbitMQ routing key
 *   bridge.inbound[0].to-topic       = Artemis topic to publish to
 *
 * ── Monitor (optional in-bridge API feed) ─────────────────────────
 *   bridge.monitor.enabled = true|false (default false)
 *   bridge.monitor.from-exchange  = RabbitMQ exchange to monitor
 *   bridge.monitor.routing-key    = routing key to watch
 *   bridge.monitor.queue          = queue name for the monitor
 *
 * Adding a new route = adding config only. Zero code changes needed.
 */
@Component
@ConfigurationProperties(prefix = "bridge")
public class BridgeConfig {

    // ── Pipeline: ordered sink list ───────────────────────────────
    private String pipeline = "kafka,rabbitmq,mqtt";

    // ── Encryption ────────────────────────────────────────────────
    private Encrypt encrypt = new Encrypt();

    // ── Outbound: Artemis topics + Kafka ──────────────────────────
    private String artemisTopics;
    private Kafka kafka = new Kafka();

    // ── Sinks ─────────────────────────────────────────────────────
    private RabbitmqOut rabbitmqOut = new RabbitmqOut();
    private MqttSink mqtt = new MqttSink();

    // ── Relay: Kafka → RabbitMQ (one entry per route, optional) ──
    private List<ForwardRoute> forward = new ArrayList<>();

    // ── Inbound: RabbitMQ → Artemis (one entry per route) ────────
    private List<InboundRoute> inbound = new ArrayList<>();

    // ── Monitor: optional in-bridge API feed ──────────────────────
    private Monitor monitor = new Monitor();

    // ── Getters / Setters ─────────────────────────────────────────

    public String getPipeline() { return pipeline; }
    public void setPipeline(String pipeline) { this.pipeline = pipeline; }

    public Encrypt getEncrypt() { return encrypt; }
    public void setEncrypt(Encrypt encrypt) { this.encrypt = encrypt; }

    public String getArtemisTopics() { return artemisTopics; }
    public void setArtemisTopics(String artemisTopics) { this.artemisTopics = artemisTopics; }

    public Kafka getKafka() { return kafka; }
    public void setKafka(Kafka kafka) { this.kafka = kafka; }

    public RabbitmqOut getRabbitmqOut() { return rabbitmqOut; }
    public void setRabbitmqOut(RabbitmqOut rabbitmqOut) { this.rabbitmqOut = rabbitmqOut; }

    public MqttSink getMqtt() { return mqtt; }
    public void setMqtt(MqttSink mqtt) { this.mqtt = mqtt; }

    public List<ForwardRoute> getForward() { return forward; }
    public void setForward(List<ForwardRoute> forward) { this.forward = forward; }

    public List<InboundRoute> getInbound() { return inbound; }
    public void setInbound(List<InboundRoute> inbound) { this.inbound = inbound; }

    public Monitor getMonitor() { return monitor; }
    public void setMonitor(Monitor monitor) { this.monitor = monitor; }

    // ── Inner: Encryption config ──────────────────────────────────
    public static class Encrypt {
        private boolean enabled = true;   // always on by default

        public boolean isEnabled() { return enabled; }
        public void setEnabled(boolean enabled) { this.enabled = enabled; }
    }

    // ── Inner: RabbitMQ outbound sink ────────────────────────────
    public static class RabbitmqOut {
        private String exchange   = "amq.topic";
        private String routingKey = "tms.scada.pas";

        public String getExchange()   { return exchange; }
        public void setExchange(String exchange) { this.exchange = exchange; }

        public String getRoutingKey()   { return routingKey; }
        public void setRoutingKey(String routingKey) { this.routingKey = routingKey; }
    }

    // ── Inner: MQTT direct sink ───────────────────────────────────
    public static class MqttSink {
        private String brokerUrl;
        private String topic    = "tms/scada/pas";
        private int    qos      = 1;
        private String clientId = "camel-paho-bridge";
        private String username;
        private String password;

        public String getBrokerUrl()  { return brokerUrl; }
        public void setBrokerUrl(String brokerUrl) { this.brokerUrl = brokerUrl; }

        public String getTopic()  { return topic; }
        public void setTopic(String topic) { this.topic = topic; }

        public int getQos()  { return qos; }
        public void setQos(int qos) { this.qos = qos; }

        public String getClientId()  { return clientId; }
        public void setClientId(String clientId) { this.clientId = clientId; }

        public String getUsername()  { return username; }
        public void setUsername(String username) { this.username = username; }

        public String getPassword()  { return password; }
        public void setPassword(String password) { this.password = password; }
    }

    // ── Inner: Monitor config ─────────────────────────────────────
    public static class Monitor {
        private boolean enabled      = false;
        private String fromExchange  = "amq.topic";
        private String routingKey    = "tms.scada.pas";
        private String queue         = "scada.monitor.queue";

        public boolean isEnabled()  { return enabled; }
        public void setEnabled(boolean enabled) { this.enabled = enabled; }

        public String getFromExchange()  { return fromExchange; }
        public void setFromExchange(String fromExchange) { this.fromExchange = fromExchange; }

        public String getRoutingKey()  { return routingKey; }
        public void setRoutingKey(String routingKey) { this.routingKey = routingKey; }

        public String getQueue()  { return queue; }
        public void setQueue(String queue) { this.queue = queue; }
    }

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
