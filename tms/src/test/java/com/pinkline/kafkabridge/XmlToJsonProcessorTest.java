package com.pinkline.kafkabridge;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.pinkline.kafkabridge.processor.JsonToXmlProcessor;
import com.pinkline.kafkabridge.processor.XmlToJsonProcessor;
import org.apache.camel.Exchange;
import org.apache.camel.impl.DefaultCamelContext;
import org.apache.camel.support.DefaultExchange;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Verifies the generic, schema-agnostic XML <-> JSON converters.
 *
 * The JSON mirrors the XML structure exactly — no fixed envelope, no
 * message-type branches. Run: mvn test (no broker needed).
 */
class XmlToJsonProcessorTest {

    private static final ObjectMapper json = new ObjectMapper();
    private XmlToJsonProcessor xmlToJson;
    private JsonToXmlProcessor jsonToXml;
    private DefaultCamelContext ctx;

    @BeforeEach
    void setup() {
        xmlToJson = new XmlToJsonProcessor();
        jsonToXml = new JsonToXmlProcessor();
        ctx = new DefaultCamelContext();
    }

    @Test
    void xmlToJson_mirrorsStructure() throws Exception {
        String xml =
            "<message>" +
            "  <type>UpdateAlarm</type>" +
            "  <creatorId>ScateX</creatorId>" +
            "  <count>3</count>" +
            "</message>";

        Exchange ex = newExchange();
        ex.getIn().setBody(xml);
        xmlToJson.process(ex);

        JsonNode root = json.readTree(ex.getIn().getBody(String.class));
        assertEquals("UpdateAlarm", root.get("type").asText());
        assertEquals("ScateX", root.get("creatorId").asText());
        assertEquals("3", root.get("count").asText());
        assertEquals("application/json", ex.getIn().getHeader("Content-Type"));
    }

    @Test
    void jsonToXml_usesRootNameFromHeader() throws Exception {
        Exchange ex = newExchange();
        ex.getIn().setBody("{\"type\":\"UpdateAlarm\",\"creatorId\":\"ScateX\"}");
        ex.getIn().setHeader("xmlRootName", "RSAEMessage");
        jsonToXml.process(ex);

        String xml = ex.getIn().getBody(String.class);
        assertTrue(xml.startsWith("<RSAEMessage"), xml);
        assertTrue(xml.contains("<type>UpdateAlarm</type>"), xml);
        assertEquals("application/xml", ex.getIn().getHeader("Content-Type"));
    }

    @Test
    void jsonToXml_unwrapsSingleRootWhenNoHeader() throws Exception {
        Exchange ex = newExchange();
        ex.getIn().setBody("{\"RSAEMessage\":{\"type\":\"UpdateAlarm\"}}");
        jsonToXml.process(ex);

        String xml = ex.getIn().getBody(String.class);
        assertTrue(xml.startsWith("<RSAEMessage"), xml);
        assertTrue(xml.contains("<type>UpdateAlarm</type>"), xml);
    }

    @Test
    void roundTrip_xmlToJsonToXml() throws Exception {
        String xml = "<RSAEMessage><type>UpdateAlarm</type><creatorId>ScateX</creatorId></RSAEMessage>";

        Exchange ex = newExchange();
        ex.getIn().setBody(xml);
        xmlToJson.process(ex);
        ex.getIn().setHeader("xmlRootName", "RSAEMessage");
        jsonToXml.process(ex);

        String out = ex.getIn().getBody(String.class);
        assertTrue(out.contains("<type>UpdateAlarm</type>"), out);
        assertTrue(out.contains("<creatorId>ScateX</creatorId>"), out);
    }

    private Exchange newExchange() {
        return new DefaultExchange(ctx);
    }
}
