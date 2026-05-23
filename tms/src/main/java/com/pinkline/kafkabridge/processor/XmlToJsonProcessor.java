package com.pinkline.kafkabridge.processor;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.xml.XmlMapper;
import org.apache.camel.Exchange;
import org.apache.camel.Processor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * XmlToJsonProcessor
 *
 * Generic, schema-agnostic XML → JSON converter.
 *
 * Reads whatever XML arrives into a Jackson tree and re-serializes it as JSON.
 * No POJOs, no message-type branches, no fixed envelope — the JSON mirrors the
 * structure of the incoming XML exactly. Add or change a TMS message format and
 * this processor needs no changes.
 */
public class XmlToJsonProcessor implements Processor {

    private static final Logger log = LoggerFactory.getLogger(XmlToJsonProcessor.class);

    private static final XmlMapper xmlMapper = new XmlMapper();
    private static final ObjectMapper jsonMapper = new ObjectMapper();

    @Override
    public void process(Exchange exchange) throws Exception {
        String xml = exchange.getIn().getBody(String.class);

        if (xml == null || xml.isBlank()) {
            throw new IllegalArgumentException("Empty XML message received");
        }

        JsonNode tree = xmlMapper.readTree(xml);
        String json = jsonMapper.writeValueAsString(tree);

        exchange.getIn().setBody(json);
        exchange.getIn().setHeader("Content-Type", "application/json");
        log.debug("XML converted to JSON ({} chars)", json.length());
    }
}
