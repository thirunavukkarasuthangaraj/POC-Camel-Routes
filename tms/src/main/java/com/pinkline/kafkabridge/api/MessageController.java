package com.pinkline.kafkabridge.api;

import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;

/**
 * REST API for received SCADA messages.
 *
 * GET /api/messages                       — all messages (JSON array)
 * GET /api/messages?schema=ATRTimeTableMsg — only that feed
 * GET /api/messages?topic=TMS.PISInfo      — only that source topic
 * GET /api/messages/stream                 — live Server-Sent Events stream
 */
@RestController
@RequestMapping("/api/messages")
@CrossOrigin("*")
public class MessageController {

    private final MessageStore store;
    private final ExecutorService executor = Executors.newCachedThreadPool();

    public MessageController(MessageStore store) {
        this.store = store;
    }

    /** Returns received messages, optionally filtered by topic and/or schema. */
    @GetMapping
    public List<MessageStore.ScadaMessage> get(
            @RequestParam(required = false) String topic,
            @RequestParam(required = false) String schema) {
        return store.find(topic, schema);
    }

    /** Real-time SSE stream — browser receives each message as it arrives */
    @GetMapping(value = "/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter stream() {
        SseEmitter emitter = new SseEmitter(0L); // no timeout

        // Send existing messages first
        executor.execute(() -> {
            Consumer<MessageStore.ScadaMessage> listener = msg -> {
                try {
                    emitter.send(SseEmitter.event()
                            .name("message")
                            .data(msg));
                } catch (IOException e) {
                    emitter.complete();
                }
            };

            // replay existing
            store.getAll().forEach(listener);

            // subscribe to new ones
            store.subscribe(listener);
            emitter.onCompletion(() -> store.unsubscribe(listener));
            emitter.onTimeout(() -> store.unsubscribe(listener));
        });

        return emitter;
    }
}
