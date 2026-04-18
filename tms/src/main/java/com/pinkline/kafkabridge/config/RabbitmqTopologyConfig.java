package com.pinkline.kafkabridge.config;

import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.TopicExchange;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * RabbitmqTopologyConfig
 *
 * Auto-declares the durable queue + binding used by the monitor route.
 * Spring AMQP's RabbitAdmin picks these beans up at startup and
 * creates them on the broker if they don't already exist.
 *
 * Why this exists:
 *   Without this, the monitor route consumer starts before the queue
 *   exists, spams NOT_FOUND errors in a retry loop, and /api/messages
 *   stays empty until an operator manually creates the queue via
 *   RabbitMQ management API. With this config, fresh RabbitMQ instances
 *   Just Work on first start.
 *
 * Gating:
 *   Only runs when bridge.monitor.enabled=true (matches KafkaBridgeRoutes
 *   which only registers the consumer when the same flag is true).
 */
@Configuration
public class RabbitmqTopologyConfig {

    private final BridgeConfig config;

    public RabbitmqTopologyConfig(BridgeConfig config) {
        this.config = config;
    }

    @Bean
    public TopicExchange monitorExchange() {
        // Durable topic exchange — amq.topic is built-in, but declaring it
        // idempotently is safe and makes the intent explicit.
        return new TopicExchange(config.getMonitor().getFromExchange(), true, false);
    }

    @Bean
    public Queue monitorQueue() {
        if (!config.getMonitor().isEnabled()) {
            // Return a non-durable throwaway when monitor is disabled so
            // RabbitAdmin doesn't touch the broker for an unused queue.
            return new Queue("bridge.monitor.disabled", false, true, true);
        }
        return new Queue(config.getMonitor().getQueue(), true, false, false);
    }

    @Bean
    public Binding monitorBinding(Queue monitorQueue, TopicExchange monitorExchange) {
        return BindingBuilder.bind(monitorQueue)
                .to(monitorExchange)
                .with(config.getMonitor().getRoutingKey());
    }
}
