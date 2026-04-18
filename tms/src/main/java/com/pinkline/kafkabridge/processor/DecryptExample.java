package com.pinkline.kafkabridge.processor;

import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

/**
 * DecryptExample
 *
 * ─────────────────────────────────────────────────────────────────────────
 * FOR THE SCADA TEAM — how to decrypt messages received from Kafka / MQTT
 * ─────────────────────────────────────────────────────────────────────────
 *
 * WIRE FORMAT (what the Bridge sends):
 *
 *   Byte 0–11   : IV   (12 bytes, random, different for every message)
 *   Byte 12–end : Ciphertext + 16-byte GCM authentication tag (appended by JCE)
 *
 *   Total minimum size: 12 + 1 (data) + 16 (GCM tag) = 29 bytes
 *
 * ALGORITHM:
 *   AES-256-GCM / NoPadding
 *   Key: 256-bit (32 bytes), shared as Base64 string via SCADA_AES_KEY env var
 *
 * HOW TO USE:
 *   1. Get the SCADA_AES_KEY from the Bridge server admin (same key must be used)
 *   2. Set it as an environment variable on the SCADA server:
 *        export SCADA_AES_KEY=<base64-256bit-key>
 *   3. Call DecryptExample.decrypt(payload) with the raw byte[] from Kafka/MQTT
 *   4. Returns the JSON string: {"messageType":"TMS_PAS_UPDATE", ...}
 *
 * IMPORTANT:
 *   - The GCM tag is verified automatically during decryption
 *   - If the message was tampered → AEADBadTagException is thrown
 *   - Always treat AEADBadTagException as a security alert — log and discard
 */
public class DecryptExample {

    private static final String ENV_KEY     = "SCADA_AES_KEY";
    private static final int    IV_LENGTH   = 12;
    private static final int    GCM_TAG_BIT = 128;

    /**
     * Decrypt a byte[] payload received from Kafka topic tms.scada.encrypted
     * or from MQTT topic tms/scada/pas.
     *
     * @param payload  raw byte[] from Kafka/MQTT
     * @return         JSON string — ICD format TMS_PAS_UPDATE
     * @throws Exception  if decryption fails or key not set
     */
    public static String decrypt(byte[] payload) throws Exception {

        // 1. Load AES-256 key from environment variable
        String keyB64 = System.getenv(ENV_KEY);
        if (keyB64 == null || keyB64.isBlank()) {
            throw new IllegalStateException(
                "SCADA_AES_KEY environment variable is not set. " +
                "Get the key from the Bridge server admin.");
        }

        byte[] keyBytes = Base64.getDecoder().decode(keyB64.trim());
        if (keyBytes.length != 32) {
            throw new IllegalStateException(
                "SCADA_AES_KEY must decode to 32 bytes (256-bit). " +
                "Current: " + keyBytes.length + " bytes.");
        }

        SecretKey secretKey = new SecretKeySpec(keyBytes, "AES");

        // 2. Extract IV (first 12 bytes)
        byte[] iv = new byte[IV_LENGTH];
        System.arraycopy(payload, 0, iv, 0, IV_LENGTH);

        // 3. Extract ciphertext (everything after IV, includes 16-byte GCM tag at end)
        byte[] ciphertext = new byte[payload.length - IV_LENGTH];
        System.arraycopy(payload, IV_LENGTH, ciphertext, 0, ciphertext.length);

        // 4. Decrypt — GCM automatically verifies the authentication tag
        //    If message was tampered → throws AEADBadTagException (security alert)
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, secretKey,
                new GCMParameterSpec(GCM_TAG_BIT, iv));

        byte[] plaintext = cipher.doFinal(ciphertext);

        return new String(plaintext, StandardCharsets.UTF_8);
    }

    // ── Quick test main — verifies encrypt/decrypt round-trip ─────────────
    public static void main(String[] args) throws Exception {

        // 1. Set a test key (32 bytes)
        String testKey = Base64.getEncoder().encodeToString(new byte[32]);
        System.setProperty("SCADA_AES_KEY_TEST", testKey);

        // 2. Simulate a payload from EncryptProcessor
        String originalJson =
            "{\"messageType\":\"TMS_PAS_UPDATE\",\"platformPredictions\":[" +
            "{\"platformId\":\"PL2201\"}]}";

        EncryptProcessor enc = new EncryptProcessor();

        org.apache.camel.impl.DefaultCamelContext ctx =
                new org.apache.camel.impl.DefaultCamelContext();
        org.apache.camel.support.DefaultExchange exchange =
                new org.apache.camel.support.DefaultExchange(ctx);
        exchange.getIn().setBody(originalJson);

        enc.process(exchange);

        byte[] payload = exchange.getIn().getBody(byte[].class);
        System.out.println("Encrypted payload length: " + payload.length + " bytes");

        // 3. Decrypt
        String decrypted = decrypt(payload);
        System.out.println("Decrypted JSON: " + decrypted);
        System.out.println("Match: " + originalJson.equals(decrypted));
    }
}
