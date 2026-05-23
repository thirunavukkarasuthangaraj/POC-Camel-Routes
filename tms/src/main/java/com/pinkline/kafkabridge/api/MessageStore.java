package com.pinkline.kafkabridge.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Component;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Consumer;

/**
 * In-memory store for decrypted SCADA messages received from RabbitMQ.
 * Holds the last 100 messages and notifies SSE subscribers on each new one.
 *
 * All TMS feeds are merged into one stream, so each message carries its
 * source topic plus the message {@code schema} (parsed once from the JSON
 * {@code hdr.schema} at ingest) to allow viewing a single feed via filters.
 */
@Component
public class MessageStore {

    private static final int MAX_MESSAGES = 100;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    public record ScadaMessage(long id, String receivedAt, String topic, String schema, String json) {}

    private final CopyOnWriteArrayList<ScadaMessage> messages = new CopyOnWriteArrayList<>();
    private final CopyOnWriteArrayList<Consumer<ScadaMessage>> subscribers = new CopyOnWriteArrayList<>();
    private long counter = 0;

    public synchronized void add(String topic, String json) {
        ScadaMessage msg = new ScadaMessage(++counter, Instant.now().toString(), topic, extractSchema(json), json);
        messages.add(msg);
        if (messages.size() > MAX_MESSAGES) messages.remove(0);
        subscribers.forEach(s -> s.accept(msg));
    }

    public List<ScadaMessage> getAll() {
        return new ArrayList<>(messages);
    }

    /**
     * Returns messages matching the given topic and/or schema. Both filters
     * are optional and case-insensitive: {@code topic} is a substring match on
     * the source topic; {@code schema} is a substring match on the parsed
     * {@code hdr.schema}. Blank/null filters are ignored.
     */
    public List<ScadaMessage> find(String topic, String schema) {
        return messages.stream()
                .filter(m -> matches(m.topic(), topic))
                .filter(m -> matches(m.schema(), schema))
                .toList();
    }

    public void subscribe(Consumer<ScadaMessage> listener) {
        subscribers.add(listener);
    }

    public void unsubscribe(Consumer<ScadaMessage> listener) {
        subscribers.remove(listener);
    }

    /** Pull the message schema from the JSON {@code hdr.schema} field, or null. */
    private static String extractSchema(String json) {
        if (json == null || json.isBlank()) return null;
        try {
            JsonNode schema = MAPPER.readTree(json).path("hdr").path("schema");
            return schema.isMissingNode() || schema.isNull() ? null : schema.asText();
        } catch (Exception e) {
            return null;
        }
    }

    /** True if filter is blank, or value contains filter (case-insensitive). */
    private static boolean matches(String value, String filter) {
        if (filter == null || filter.isBlank()) return true;
        return value != null && value.toLowerCase().contains(filter.toLowerCase());
    }
}
