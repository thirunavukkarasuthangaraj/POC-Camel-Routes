"""
Mock SCADA API — subscribes to MQTT, decrypts AES-GCM payload,
exposes received messages via HTTP.

Endpoints:
  GET  /            → health + stats
  GET  /messages    → last 100 decrypted messages (JSON)
  GET  /stream      → live Server-Sent Events stream

Environment variables:
  SCADA_AES_KEY   Base64-encoded 32-byte AES-256 key (must match bridge)
  MQTT_HOST       default: rabbitmq
  MQTT_PORT       default: 1883
  MQTT_USER       default: thiru
  MQTT_PASS       default: password
  MQTT_TOPIC      default: tms/scada/pas
"""
import base64
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from queue import Queue, Empty

import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Flask, Response, jsonify


MQTT_HOST  = os.getenv("MQTT_HOST",  "rabbitmq")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER  = os.getenv("MQTT_USER",  "thiru")
MQTT_PASS  = os.getenv("MQTT_PASS",  "password")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "tms/scada/pas")

AES_KEY_B64 = os.getenv("SCADA_AES_KEY", "")
AES_KEY = base64.b64decode(AES_KEY_B64) if AES_KEY_B64 else None

app = Flask(__name__)
messages = deque(maxlen=100)
sse_queues: list[Queue] = []
stats = {"received": 0, "decrypt_ok": 0, "decrypt_failed": 0}


def decrypt(payload: bytes) -> dict:
    if AES_KEY is None:
        return {"raw_base64": base64.b64encode(payload).decode(),
                "note": "SCADA_AES_KEY not set — cannot decrypt"}
    if len(payload) < 28:
        raise ValueError(f"payload too short: {len(payload)} bytes")
    iv, ciphertext = payload[:12], payload[12:]
    plaintext = AESGCM(AES_KEY).decrypt(iv, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))


def on_message(client, userdata, msg):
    stats["received"] += 1
    entry = {
        "id": stats["received"],
        "receivedAt": datetime.now(timezone.utc).isoformat(),
        "topic": msg.topic,
        "payloadSize": len(msg.payload),
    }
    try:
        entry["decoded"] = decrypt(msg.payload)
        stats["decrypt_ok"] += 1
    except Exception as e:
        entry["error"] = str(e)
        entry["raw_base64"] = base64.b64encode(msg.payload).decode()
        stats["decrypt_failed"] += 1

    messages.appendleft(entry)
    for q in sse_queues[:]:
        try:
            q.put_nowait(entry)
        except Exception:
            pass


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(MQTT_TOPIC)
        print(f"[scada-api] connected to MQTT {MQTT_HOST}:{MQTT_PORT}, "
              f"subscribed to '{MQTT_TOPIC}'", flush=True)
    else:
        print(f"[scada-api] MQTT connect failed, rc={rc}", flush=True)


def on_disconnect(client, userdata, rc):
    print(f"[scada-api] MQTT disconnected rc={rc}, auto-reconnecting", flush=True)


def mqtt_loop():
    while True:
        try:
            client = mqtt.Client(client_id="scada-api-mock")
            client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.on_message = on_message
            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            print(f"[scada-api] MQTT loop error: {e} — retrying in 5s", flush=True)
            time.sleep(5)


@app.route("/")
def index():
    return jsonify({
        "service": "mock-scada-api",
        "status": "running",
        "mqtt": {
            "host": MQTT_HOST, "port": MQTT_PORT,
            "topic": MQTT_TOPIC, "user": MQTT_USER,
        },
        "stats": stats,
        "endpoints": ["/messages", "/stream"],
    })


@app.route("/messages")
def get_messages():
    return jsonify(list(messages))


@app.route("/stream")
def stream():
    def gen():
        q: Queue = Queue()
        sse_queues.append(q)
        try:
            for entry in list(messages)[:20]:
                yield f"data: {json.dumps(entry)}\n\n"
            while True:
                try:
                    entry = q.get(timeout=25)
                    yield f"data: {json.dumps(entry)}\n\n"
                except Empty:
                    yield ": keepalive\n\n"
        finally:
            try:
                sse_queues.remove(q)
            except ValueError:
                pass

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    threading.Thread(target=mqtt_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8090, threaded=True)
