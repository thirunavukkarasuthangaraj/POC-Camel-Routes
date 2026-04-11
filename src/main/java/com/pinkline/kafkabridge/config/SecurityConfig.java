package com.pinkline.kafkabridge.config;

import org.apache.activemq.ActiveMQSslConnectionFactory;
import org.apache.camel.component.activemq.ActiveMQComponent;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class SecurityConfig {

    @Value("${camel.component.activemq.broker-url}")
    private String artemisUrl;

    @Value("${camel.component.activemq.username}")
    private String artemisUser;

    @Value("${camel.component.activemq.password}")
    private String artemisPass;

    @Bean
    public ActiveMQComponent activeMQComponent() {
        ActiveMQSslConnectionFactory factory = new ActiveMQSslConnectionFactory(artemisUrl);
        factory.setUserName(artemisUser);
        factory.setPassword(artemisPass);
        factory.setWatchTopicAdvisories(false);

        ActiveMQComponent component = new ActiveMQComponent();
        component.setConnectionFactory(factory);
        return component;
    }
}
