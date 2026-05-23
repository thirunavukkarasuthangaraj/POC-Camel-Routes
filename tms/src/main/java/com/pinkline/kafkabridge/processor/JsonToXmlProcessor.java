package com.pinkline.kafkabridge.processor;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.xml.XmlMapper;
import org.apache.camel.Exchange;
import org.apache.camel.Processor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * JsonToXmlProcessor
 *
 * Generic, schema-agnostic JSON → XML converter — the reverse of
 * {@link XmlToJsonProcessor}.
 *
 * The XML root element name is taken from the "xmlRootName" header (set from
 * config, never hardcoded here). If the header is absent and the JSON is a
 * single-field object, that field is used as the root and unwrapped. Otherwise
 * the message is forwarded unchanged.
 */
public class JsonToXmlProcessor implements Processor {

    private static final Logger log = LoggerFactory.getLogger(JsonToXmlProcessor.class);

    private static final ObjectMapper jsonMapper = new ObjectMapper();
    private static final XmlMapper xmlMapper = new XmlMapper();

    @Override
    public void process(Exchange exchange) throws Exception {
        String json = exchange.getIn().getBody(String.class);

        if (json == null || json.isBlank()) {
            log.warn("JsonToXml — empty input, skipping conversion");
            return;
        }

        try {
            JsonNode root = jsonMapper.readTree(json);

            // Root element name comes from config via the header — no static name here.
            String rootName = exchange.getIn().getHeader("xmlRootName", String.class);

            // No header supplied: if the JSON is a single-field object, use that
            // field as the root and serialize its value.
            if ((rootName == null || rootName.isBlank())
                    && root.isObject() && root.size() == 1) {
                String only = root.fieldNames().next();
                rootName = only;
                root = root.get(only);
            }

            if (rootName == null || rootName.isBlank()) {
                log.warn("JsonToXml — no xmlRootName header and JSON is not single-rooted, "
                        + "forwarding JSON unchanged");
                return;
            }

            String xml = xmlMapper.writer()
                    .withRootName(rootName)
                    .writeValueAsString(root);

            exchange.getIn().setBody(xml);
            exchange.getIn().setHeader("Content-Type", "application/xml");
            log.debug("JsonToXml — converted JSON to <{}> XML ({} chars)", rootName, xml.length());

        } catch (Exception e) {
            log.error("JsonToXml — conversion failed, forwarding raw JSON: {}", e.getMessage());
            // Non-fatal: pass JSON as-is rather than dropping the message.
        }
    }
}
