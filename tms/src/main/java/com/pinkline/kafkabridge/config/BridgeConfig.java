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
 * ── Staged reverse (RabbitMQ → Kafka → Artemis) ──────────────────
 *   bridge.scada-source[0].*  = RabbitMQ source → Kafka (Stage B)
 *   bridge.artemis-sink[0].*  = Kafka → Artemis sink (Stage D)
 *   Stage C is bridge.reverse-kafka.* above.
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
    // Default "" so getArtemisTopics().split(",") never NPEs even when
    // BRIDGE_INPUT_FROM_KAFKA=true is set without unsetting this property.
    private String artemisTopics = "";
    private Kafka kafka = new Kafka();

    // ── Kafka-sourced input: consume Kafka instead of Artemis ─────
    // When inputKafka.enabled=true the Artemis-direct loop is SKIPPED.
    private InputKafka inputKafka = new InputKafka();

    // ── Reverse Kafka path (Stage C): decrypt + optional XML ──────
    private ReverseKafka reverseKafka = new ReverseKafka();

    // ── Sinks ─────────────────────────────────────────────────────
    private RabbitmqOut rabbitmqOut = new RabbitmqOut();
    private MqttSink mqtt = new MqttSink();

    // ── Relay: Kafka → RabbitMQ (one entry per route, optional) ──
    private List<ForwardRoute> forward = new ArrayList<>();

    // ── Inbound: RabbitMQ → Artemis (one entry per route) ────────
    private List<InboundRoute> inbound = new ArrayList<>();

    // ── ScadaSource: RabbitMQ → Kafka (Stage B of reverse flow) ──
    private List<ScadaSource> scadaSource = new ArrayList<>();

    // ── ArtemisSink: Kafka → Artemis (Stage D of reverse flow) ───
    private List<ArtemisSink> artemisSink = new ArrayList<>();

    // ── Monitor: optional in-bridge API feed ──────────────────────
    private Monitor monitor = new Monitor();

    // ── AlarmState: compacted Kafka state topic (flood-control) ──
    // When enabled, the reverse route fans out one record per alarm
    // into a compacted Kafka topic keyed by alarmId. SCADA reconnect
    // logic can then snapshot the latest state without replaying full
    // event history.
    private AlarmState alarmState = new AlarmState();

    // ── Getters / Setters ─────────────────────────────────────────

    public String getPipeline() { return pipeline; }
    public void setPipeline(String pipeline) { this.pipeline = pipeline; }

    public Encrypt getEncrypt() { return encrypt; }
    public void setEncrypt(Encrypt encrypt) { this.encrypt = encrypt; }

    public String getArtemisTopics() { return artemisTopics; }
    public void setArtemisTopics(String artemisTopics) { this.artemisTopics = artemisTopics; }

    public Kafka getKafka() { return kafka; }
    public void setKafka(Kafka kafka) { this.kafka = kafka; }

    public InputKafka getInputKafka() { return inputKafka; }
    public void setInputKafka(InputKafka inputKafka) { this.inputKafka = inputKafka; }

    public ReverseKafka getReverseKafka() { return reverseKafka; }
    public void setReverseKafka(ReverseKafka reverseKafka) { this.reverseKafka = reverseKafka; }

    public RabbitmqOut getRabbitmqOut() { return rabbitmqOut; }
    public void setRabbitmqOut(RabbitmqOut rabbitmqOut) { this.rabbitmqOut = rabbitmqOut; }

    public MqttSink getMqtt() { return mqtt; }
    public void setMqtt(MqttSink mqtt) { this.mqtt = mqtt; }

    public List<ForwardRoute> getForward() { return forward; }
    public void setForward(List<ForwardRoute> forward) { this.forward = forward; }

    public List<InboundRoute> getInbound() { return inbound; }
    public void setInbound(List<InboundRoute> inbound) { this.inbound = inbound; }

    public List<ScadaSource> getScadaSource() { return scadaSource; }
    public void setScadaSource(List<ScadaSource> scadaSource) { this.scadaSource = scadaSource; }

    public List<ArtemisSink> getArtemisSink() { return artemisSink; }
    public void setArtemisSink(List<ArtemisSink> artemisSink) { this.artemisSink = artemisSink; }

    public Monitor getMonitor() { return monitor; }
    public void setMonitor(Monitor monitor) { this.monitor = monitor; }

    public AlarmState getAlarmState() { return alarmState; }
    public void setAlarmState(AlarmState alarmState) { this.alarmState = alarmState; }

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

    // ── Inner: AlarmState compacted topic ─────────────────────────
    public static class AlarmState {
        private boolean enabled = false;
        private String  topic   = "scada.tms.alarms.state";

        public boolean isEnabled() { return enabled; }
        public void setEnabled(boolean enabled) { this.enabled = enabled; }
        public String getTopic() { return topic; }
        public void setTopic(String topic) { this.topic = topic; }
    }

    // ── Inner: Kafka config ───────────────────────────────────────
    public static class Kafka {
        private String topic;

        public String getTopic() { return topic; }
        public void setTopic(String topic) { this.topic = topic; }
    }

    // ── Inner: Kafka-sourced input (Kafka source instead of Artemis) ──
    // When enabled=true, skip the Artemis topic loop and consume from
    // this Kafka topic. The XmlToJson + Encrypt processors run as today.
    public static class InputKafka {
        private boolean enabled = false;
        private String  topic         = "tms.raw";
        private String  consumerGroup = "pas-bridge-transform";

        public boolean isEnabled() { return enabled; }
        public void setEnabled(boolean enabled) { this.enabled = enabled; }
        public String getTopic() { return topic; }
        public void setTopic(String topic) { this.topic = topic; }
        public String getConsumerGroup() { return consumerGroup; }
        public void setConsumerGroup(String consumerGroup) { this.consumerGroup = consumerGroup; }
    }

    // ── Inner: Reverse Kafka path (Stage C of staged reverse flow) ──
    // Reads encrypted JSON from inputTopic, decrypts, optionally
    // converts JSON → XML, and writes outputTopic. Drained by an
    // artemis-sink route into TMS-side Artemis.
    public static class ReverseKafka {
        private boolean enabled = false;
        private String  inputTopic    = "scada.tms.raw";
        private String  outputTopic   = "scada.tms.processed";
        private String  consumerGroup = "pas-bridge-reverse";
        private boolean convertToXml  = false;
        // SCADA→TMS direction: defaults true (decrypt incoming) for back-compat,
        // set false when SCADA publishes plain JSON.
        private boolean encryptEnabled = true;

        public boolean isEnabled() { return enabled; }
        public void setEnabled(boolean enabled) { this.enabled = enabled; }
        public String getInputTopic() { return inputTopic; }
        public void setInputTopic(String inputTopic) { this.inputTopic = inputTopic; }
        public String getOutputTopic() { return outputTopic; }
        public void setOutputTopic(String outputTopic) { this.outputTopic = outputTopic; }
        public String getConsumerGroup() { return consumerGroup; }
        public void setConsumerGroup(String consumerGroup) { this.consumerGroup = consumerGroup; }
        public boolean isConvertToXml() { return convertToXml; }
        public void setConvertToXml(boolean convertToXml) { this.convertToXml = convertToXml; }
        public boolean isEncryptEnabled() { return encryptEnabled; }
        public void setEncryptEnabled(boolean encryptEnabled) { this.encryptEnabled = encryptEnabled; }
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
        private String queue;
        private String toTopic;
        // true  = convert RSAE JSON → XML before publishing to Artemis
        // false = forward JSON as-is (default; TMS consumes JSON directly)
        private boolean convertToXml = false;

        public String getFromExchange() { return fromExchange; }
        public void setFromExchange(String fromExchange) { this.fromExchange = fromExchange; }

        public String getRoutingKey() { return routingKey; }
        public void setRoutingKey(String routingKey) { this.routingKey = routingKey; }

        public String getQueue() { return queue; }
        public void setQueue(String queue) { this.queue = queue; }

        public String getToTopic() { return toTopic; }
        public void setToTopic(String toTopic) { this.toTopic = toTopic; }

        public boolean isConvertToXml() { return convertToXml; }
        public void setConvertToXml(boolean convertToXml) { this.convertToXml = convertToXml; }
    }

    // ── Inner: RabbitMQ → Kafka source (Stage B reverse) ─────────
    public static class ScadaSource {
        private String fromExchange;
        private String routingKey;
        private String queue;
        private String toKafkaTopic;

        public String getFromExchange() { return fromExchange; }
        public void setFromExchange(String fromExchange) { this.fromExchange = fromExchange; }
        public String getRoutingKey() { return routingKey; }
        public void setRoutingKey(String routingKey) { this.routingKey = routingKey; }
        public String getQueue() { return queue; }
        public void setQueue(String queue) { this.queue = queue; }
        public String getToKafkaTopic() { return toKafkaTopic; }
        public void setToKafkaTopic(String toKafkaTopic) { this.toKafkaTopic = toKafkaTopic; }
    }

    // ── Inner: Kafka → Artemis sink (Stage D reverse) ────────────
    public static class ArtemisSink {
        private String fromKafka;
        private String consumerGroup;
        private String toTopic;

        public String getFromKafka() { return fromKafka; }
        public void setFromKafka(String fromKafka) { this.fromKafka = fromKafka; }
        public String getConsumerGroup() { return consumerGroup; }
        public void setConsumerGroup(String consumerGroup) { this.consumerGroup = consumerGroup; }
        public String getToTopic() { return toTopic; }
        public void setToTopic(String toTopic) { this.toTopic = toTopic; }
    }
}
