package com.pinkline.kafkabridge;

import com.pinkline.kafkabridge.processor.EncryptProcessor;
import org.apache.camel.Exchange;
import org.apache.camel.impl.DefaultCamelContext;
import org.apache.camel.support.DefaultExchange;
import org.junit.jupiter.api.Test;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import javax.crypto.SecretKey;
import java.util.Base64;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit test for EncryptProcessor.
 *
 * The processor emits a base64-encoded String whose decoded bytes are the
 * wire format [12B IV][ciphertext + 16B GCM tag] — matching how
 * {@link com.pinkline.kafkabridge.processor.DecryptExample} consumes it.
 *
 * Verifies:
 *  1. Output is a base64 String
 *  2. Decoded wire format: [12B IV][ciphertext][16B GCM tag]
 *  3. Decryption with same key recovers original JSON
 *  4. Different messages produce different ciphertext (fresh IV)
 */
class EncryptProcessorTest {

    // Test AES-256 key (32 bytes = 256 bits, base64 encoded)
    // In real deployment this comes from SCADA_AES_KEY env variable
    private static final String TEST_KEY_B64 =
        Base64.getEncoder().encodeToString(new byte[32]); // 32 zero bytes for test

    private static final String SAMPLE_JSON =
        "{\"msgType\":\"TMS_PAS_UPDATE\",\"platform\":{\"id\":\"PL2201\"}}";

    // SCADA_AES_KEY is set by Maven Surefire (see pom.xml environmentVariables).
    // In production it is set externally: export SCADA_AES_KEY=<base64-key>

    @Test
    void testOutputIsBase64String() throws Exception {
        EncryptProcessor processor = new EncryptProcessor();
        Exchange exchange = new DefaultExchange(new DefaultCamelContext());
        exchange.getIn().setBody(SAMPLE_JSON);

        processor.process(exchange);

        Object body = exchange.getIn().getBody();
        assertInstanceOf(String.class, body, "Output must be a base64 String");
        // Must be valid base64 (throws if not)
        assertDoesNotThrow(() -> Base64.getDecoder().decode((String) body));
    }

    @Test
    void testWireFormatLength() throws Exception {
        EncryptProcessor processor = new EncryptProcessor();
        Exchange exchange = new DefaultExchange(new DefaultCamelContext());
        exchange.getIn().setBody(SAMPLE_JSON);

        processor.process(exchange);

        byte[] payload = Base64.getDecoder().decode(exchange.getIn().getBody(String.class));

        // Minimum: 12 (IV) + 1 (ciphertext) + 16 (GCM tag) = 29 bytes
        assertTrue(payload.length >= 29,
            "Payload must be at least 29 bytes (12 IV + data + 16 GCM tag)");

        System.out.println("Payload length: " + payload.length + " bytes");
    }

    @Test
    void testDecryptionRecoversOriginalJson() throws Exception {
        EncryptProcessor processor = new EncryptProcessor();
        Exchange exchange = new DefaultExchange(new DefaultCamelContext());
        exchange.getIn().setBody(SAMPLE_JSON);

        processor.process(exchange);

        byte[] payload = Base64.getDecoder().decode(exchange.getIn().getBody(String.class));

        // Extract IV (first 12 bytes)
        byte[] iv = new byte[12];
        System.arraycopy(payload, 0, iv, 0, 12);

        // Extract ciphertext (remaining bytes)
        byte[] ciphertext = new byte[payload.length - 12];
        System.arraycopy(payload, 12, ciphertext, 0, ciphertext.length);

        // Decrypt with same key
        byte[] keyBytes = Base64.getDecoder().decode(TEST_KEY_B64);
        SecretKey secretKey = new SecretKeySpec(keyBytes, "AES");

        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, secretKey,
            new GCMParameterSpec(128, iv));
        byte[] decrypted = cipher.doFinal(ciphertext);

        String recoveredJson = new String(decrypted, StandardCharsets.UTF_8);
        assertEquals(SAMPLE_JSON, recoveredJson,
            "Decrypted output must match original JSON");

        System.out.println("Decrypted successfully: " + recoveredJson);
    }

    @Test
    void testTwoMessagesHaveDifferentCiphertext() throws Exception {
        EncryptProcessor processor = new EncryptProcessor();

        Exchange ex1 = new DefaultExchange(new DefaultCamelContext());
        ex1.getIn().setBody(SAMPLE_JSON);
        processor.process(ex1);

        Exchange ex2 = new DefaultExchange(new DefaultCamelContext());
        ex2.getIn().setBody(SAMPLE_JSON);
        processor.process(ex2);

        String payload1 = ex1.getIn().getBody(String.class);
        String payload2 = ex2.getIn().getBody(String.class);

        assertNotEquals(payload1, payload2,
            "Same JSON must produce different ciphertext (different IV each time)");

        System.out.println("Verified: fresh IV per message — ciphertexts differ");
    }

}
