package com.pinkline.kafkabridge.api;

import com.pinkline.kafkabridge.config.BridgeConfig;
import org.apache.camel.ProducerTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * TmsPublishController
 *
 * Lets the SCADA simulator dashboard inject test XML into the TMS-side
 * Artemis broker over HTTP, so a demo presenter can show the forward
 * path (TMS → SCADA) flowing without shelling out to artemis-pub.
 *
 *   POST /api/tms-publish/{topic}
 *     body: raw XML string (Content-Type: text/plain or application/xml)
 *     → publishes to activemq:topic:{topic}, picked up by the
 *       outbound-* Camel route and forwarded through the pipeline.
 *
 * Topic must be one of the configured Artemis topics (allow-list driven
 * from bridge.artemis-topics in application.properties — no hardcoding).
 */
@RestController
@RequestMapping("/api/tms-publish")
@CrossOrigin("*")
public class TmsPublishController {

    private static final Logger log = LoggerFactory.getLogger(TmsPublishController.class);

    private final ProducerTemplate producer;
    private final BridgeConfig config;

    public TmsPublishController(ProducerTemplate producer, BridgeConfig config) {
        this.producer = producer;
        this.config = config;
    }

    private Set<String> allowed() {
        return Arrays.stream(config.getArtemisTopics().split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .collect(Collectors.toCollection(LinkedHashSet::new));
    }

    @PostMapping(value = "/{topic}", consumes = MediaType.ALL_VALUE)
    public ResponseEntity<Map<String, Object>> publish(
            @PathVariable String topic,
            @RequestBody String body) {
        Set<String> allow = allowed();
        if (!allow.contains(topic)) {
            return ResponseEntity.badRequest().body(Map.of(
                    "ok", false,
                    "error", "topic not in allow-list",
                    "allowed", allow));
        }
        if (body == null || body.isBlank()) {
            return ResponseEntity.badRequest().body(Map.of(
                    "ok", false,
                    "error", "empty body"));
        }
        producer.sendBody("activemq:topic:" + topic, body);
        log.info("TmsPublish — published {} chars to Artemis topic://{}", body.length(), topic);
        return ResponseEntity.ok(Map.of(
                "ok", true,
                "topic", topic,
                "size", body.length()));
    }

    @GetMapping("/topics")
    public Set<String> topics() {
        return allowed();
    }
}
