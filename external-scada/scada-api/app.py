"""
Mock SCADA API — full simulator + dashboard.

Behaviour:
  * Receives encrypted TMS messages from MQTT topic tms/scada/pas,
    decrypts with SCADA_AES_KEY, stores for viewing.
  * Publishes UpdateAlarm messages on a configurable interval
    (simulates equipment state changes).
  * Publishes KeepAlive on a configurable interval
    (configurable at runtime via /config endpoint or dashboard).
  * Handles GetAllAlarms requests (responds with SendAllAlarms).
  * Exposes a visual HTML dashboard at /.

Endpoints:
  GET  /                 → HTML dashboard (live visual view)
  GET  /api/status       → connection + timer config + stats
  GET  /api/received     → last 100 decrypted messages from TMS
  GET  /api/sent         → last 100 messages SCADA published outbound
  GET  /api/keepalives   → KeepAlive history
  GET  /api/alarms       → current in-memory alarm state table
  GET  /api/stream       → SSE stream of all events (received + sent)
  POST /api/config       → update runtime config (keepalive interval etc.)
  POST /api/publish      → manually publish an alarm (body: {id, state})
  POST /api/pause        → pause KeepAlive (simulate connection cut)
  POST /api/resume       → resume KeepAlive
  POST /api/sendall      → trigger a SendAllAlarms broadcast

ENV vars:
  SCADA_AES_KEY   Base64 32-byte key for TMS decrypt
  MQTT_HOST       default rabbitmq
  MQTT_PORT       default 1883
  MQTT_USER       default thiru
  MQTT_PASS       default password
  MQTT_TOPIC_IN   default tms/scada/pas      (TMS→SCADA, encrypted)
  MQTT_TOPIC_OUT  default scada/tms/alarms   (SCADA→TMS, plain JSON,
                                              RSAE envelope — TMS team
                                              consumes via RabbitMQ AMQP
                                              on amq.topic routing key
                                              scada.tms.alarms)
  SCADA_CREATOR_ID default ScateX
"""
import base64
import json
import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from queue import Queue, Empty

import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Flask, Response, jsonify, request, send_from_directory


# ── Config (env + runtime-mutable) ─────────────────────────────────────
MQTT_HOST  = os.getenv("MQTT_HOST",  "rabbitmq")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER  = os.getenv("MQTT_USER",  "thiru")
MQTT_PASS  = os.getenv("MQTT_PASS",  "password")

# Inbound: TMS→SCADA (encrypted PIS/alarm data bridged from TMS)
TOPIC_IN   = os.getenv("MQTT_TOPIC_IN",  "tms/scada/pas")
# Outbound: SCADA→TMS — SINGLE topic carries all 4 RSAE message types.
# The `Type` field inside the JSON envelope distinguishes them:
#   UpdateAlarm / SendAllAlarms / GetAllAlarms / KeepAlive
# MQTT publish lands in RabbitMQ via the MQTT plugin automatically —
# AMQP subscribers see it on amq.topic with routing key scada.tms.alarms
# (plugin converts / to . on the way in).
TOPIC_OUT  = os.getenv("MQTT_TOPIC_OUT", "scada/tms/alarms")

CREATOR_ID = os.getenv("SCADA_CREATOR_ID", "ScateX")

AES_KEY_B64 = os.getenv("SCADA_AES_KEY", "")
AES_KEY = base64.b64decode(AES_KEY_B64) if AES_KEY_B64 else None

# Runtime-mutable config (set via POST /api/config or dashboard)
runtime = {
    "keepalive_seconds":   30,
    "auto_alarm_seconds":  10,   # 0 = disabled
    "auto_alarm_enabled":  True,
    "keepalive_paused":    False,
    "mqtt_connected":      False,
    "last_keepalive_sent": None,
    "last_tms_received":   None,
}

# ── In-memory stores ──────────────────────────────────────────────────
messages_in  = deque(maxlen=100)
messages_out = deque(maxlen=100)
keepalives   = deque(maxlen=50)
# Current alarm state table — key = alarm.Id, value = full alarm dict
alarm_db: dict[str, dict] = {}

stats = {
    "received":     0,
    "sent_alarm":   0,
    "sent_ka":      0,
    "sent_all":     0,
    "decrypt_ok":   0,
    "decrypt_fail": 0,
}

sse_queues: list[Queue] = []

# Sample equipment for simulated alarms
EQUIPMENT = [
    "ESC_ROL_1_23", "ESC_ROL_2_27", "ESC_ROL_3_15",
    "DOOR_PLT_2_3", "DOOR_PLT_1_1", "DOOR_STN_1_MAIN",
    "ELV_STN_4_01", "ELV_STN_2_03",
    "DNVEL01.ALC.ED06", "DNVEL02.ALC.ED06",
    "PUMP_DRY_1", "PUMP_DRY_2",
    "HVAC_PLT_1", "FIRE_DETECT_OFFICE_A",
    "CCTV_PLT_2_CAM5",
]

app = Flask(__name__, static_folder="static")
mqtt_client: mqtt.Client | None = None


# ── Helpers ───────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_rsae():
    # RSAE spec timestamp format: YYYY-MM-DD HH:mm:ss.fff
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}"


def broadcast_sse(event_type: str, payload: dict):
    msg = {"type": event_type, "payload": payload, "ts": now_iso()}
    for q in sse_queues[:]:
        try:
            q.put_nowait(msg)
        except Exception:
            pass


def decrypt_payload(raw: bytes) -> dict:
    if AES_KEY is None:
        return {"raw_base64": base64.b64encode(raw).decode(),
                "note": "SCADA_AES_KEY not set — can't decrypt"}
    if len(raw) < 28:
        raise ValueError(f"payload too short: {len(raw)} bytes")
    iv, ciphertext = raw[:12], raw[12:]
    plain = AESGCM(AES_KEY).decrypt(iv, ciphertext, None)
    return json.loads(plain.decode("utf-8"))


# ── MQTT handlers ─────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    runtime["mqtt_connected"] = (rc == 0)
    if rc == 0:
        client.subscribe(TOPIC_IN)
        print(f"[scada-api] connected to {MQTT_HOST}:{MQTT_PORT}, sub '{TOPIC_IN}'", flush=True)
        broadcast_sse("connection", {"status": "connected"})
    else:
        print(f"[scada-api] MQTT connect failed rc={rc}", flush=True)
        broadcast_sse("connection", {"status": "failed", "rc": rc})


def on_disconnect(client, userdata, rc):
    runtime["mqtt_connected"] = False
    print(f"[scada-api] MQTT disconnected rc={rc}", flush=True)
    broadcast_sse("connection", {"status": "disconnected", "rc": rc})


def on_message(client, userdata, msg):
    stats["received"] += 1
    runtime["last_tms_received"] = now_iso()
    entry = {
        "id":         stats["received"],
        "receivedAt": now_iso(),
        "topic":      msg.topic,
        "payloadSize": len(msg.payload),
    }
    try:
        decoded = decrypt_payload(msg.payload)
        entry["decoded"] = decoded
        stats["decrypt_ok"] += 1

        # If TMS just sent us a GetAllAlarms, respond with SendAllAlarms
        if isinstance(decoded, dict) and decoded.get("Type") == "GetAllAlarms":
            send_all_alarms()
    except Exception as e:
        entry["error"]      = str(e)
        entry["raw_base64"] = base64.b64encode(msg.payload).decode()
        stats["decrypt_fail"] += 1

    messages_in.appendleft(entry)
    broadcast_sse("received", entry)


# ── SCADA publisher — the "sending" side ─────────────────────────────
def publish(topic: str, envelope: dict, kind: str):
    if mqtt_client is None or not runtime["mqtt_connected"]:
        return False
    payload = json.dumps(envelope).encode("utf-8")
    mqtt_client.publish(topic, payload, qos=0)
    record = {
        "id":        sum([stats["sent_alarm"], stats["sent_ka"], stats["sent_all"]]),
        "sentAt":    now_iso(),
        "topic":     topic,
        "kind":      kind,
        "envelope":  envelope,
    }
    messages_out.appendleft(record)
    broadcast_sse("sent", record)
    return True


def publish_update_alarm(alarm_id: str, state: str):
    alarm = {
        "Timestamp": now_rsae(),
        "Id":        alarm_id,
        "State":     str(state),
    }
    envelope = {
        "CreatorId": CREATOR_ID,
        "Type":      "UpdateAlarm",
        "Timestamp": now_rsae(),
        "Alarm":     alarm,
    }
    alarm_db[alarm_id] = alarm
    if publish(TOPIC_OUT, envelope, "UpdateAlarm"):
        stats["sent_alarm"] += 1
        return True
    return False


def publish_keepalive():
    if runtime["keepalive_paused"]:
        return False
    envelope = {
        "CreatorId": CREATOR_ID,
        "Type":      "KeepAlive",
        "Timestamp": now_rsae(),
    }
    ok = publish(TOPIC_OUT, envelope, "KeepAlive")
    if ok:
        stats["sent_ka"] += 1
        runtime["last_keepalive_sent"] = now_iso()
        keepalives.appendleft({"sentAt": now_iso(), "envelope": envelope})
    return ok


def send_all_alarms():
    envelope = {
        "CreatorId": CREATOR_ID,
        "Type":      "SendAllAlarms",
        "Timestamp": now_rsae(),
        "Alarms":    list(alarm_db.values()),
    }
    ok = publish(TOPIC_OUT, envelope, "SendAllAlarms")
    if ok:
        stats["sent_all"] += 1
    return ok


# ── Timers — runtime-configurable intervals ──────────────────────────
def keepalive_timer():
    while True:
        time.sleep(1)
        if runtime["keepalive_paused"] or not runtime["mqtt_connected"]:
            continue
        last = runtime["last_keepalive_sent"]
        last_dt = datetime.fromisoformat(last) if last else None
        if (last_dt is None or
                (datetime.now(timezone.utc) - last_dt).total_seconds()
                >= runtime["keepalive_seconds"]):
            publish_keepalive()


def auto_alarm_timer():
    while True:
        time.sleep(1)
        if (not runtime["auto_alarm_enabled"] or
                not runtime["mqtt_connected"] or
                runtime["auto_alarm_seconds"] <= 0):
            continue
        time.sleep(runtime["auto_alarm_seconds"] - 1)
        if runtime["auto_alarm_enabled"] and runtime["mqtt_connected"]:
            eq = random.choice(EQUIPMENT)
            state = random.choice(["0", "1", "0", "0"])  # skewed toward OK
            publish_update_alarm(eq, state)


def mqtt_loop():
    global mqtt_client
    while True:
        try:
            c = mqtt.Client(client_id=f"scada-sim-{CREATOR_ID}")
            c.username_pw_set(MQTT_USER, MQTT_PASS)
            c.on_connect    = on_connect
            c.on_disconnect = on_disconnect
            c.on_message    = on_message
            c.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            mqtt_client = c
            c.loop_forever()
        except Exception as e:
            runtime["mqtt_connected"] = False
            print(f"[scada-api] MQTT loop: {e} — retry 5s", flush=True)
            time.sleep(5)


# ── HTTP API ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "creatorId":  CREATOR_ID,
        "mqtt": {
            "host":    MQTT_HOST,
            "port":    MQTT_PORT,
            "topicIn": TOPIC_IN,
            "topicOut": TOPIC_OUT,
            "topicKeepAlive": TOPIC_KA,
            "connected": runtime["mqtt_connected"],
        },
        "runtime":    runtime,
        "stats":      stats,
        "alarmCount": len(alarm_db),
    })


@app.route("/api/received")
def api_received():
    return jsonify(list(messages_in))


@app.route("/api/sent")
def api_sent():
    return jsonify(list(messages_out))


@app.route("/api/keepalives")
def api_keepalives():
    return jsonify(list(keepalives))


@app.route("/api/alarms")
def api_alarms():
    return jsonify([
        {"Id": k, **v} for k, v in sorted(alarm_db.items())
    ])


@app.route("/api/config", methods=["POST"])
def api_config():
    body = request.get_json(force=True, silent=True) or {}
    for key in ("keepalive_seconds", "auto_alarm_seconds"):
        if key in body:
            try:
                runtime[key] = max(0, int(body[key]))
            except Exception:
                pass
    for key in ("auto_alarm_enabled", "keepalive_paused"):
        if key in body:
            runtime[key] = bool(body[key])
    broadcast_sse("config", runtime)
    return jsonify(runtime)


@app.route("/api/publish", methods=["POST"])
def api_publish():
    body = request.get_json(force=True, silent=True) or {}
    alarm_id = body.get("id") or random.choice(EQUIPMENT)
    state    = str(body.get("state", "1"))
    ok = publish_update_alarm(alarm_id, state)
    return jsonify({"ok": ok, "id": alarm_id, "state": state})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    runtime["keepalive_paused"] = True
    broadcast_sse("config", runtime)
    return jsonify(runtime)


@app.route("/api/resume", methods=["POST"])
def api_resume():
    runtime["keepalive_paused"] = False
    broadcast_sse("config", runtime)
    return jsonify(runtime)


@app.route("/api/sendall", methods=["POST"])
def api_sendall():
    ok = send_all_alarms()
    return jsonify({"ok": ok, "alarmCount": len(alarm_db)})


@app.route("/api/stream")
def api_stream():
    def gen():
        q: Queue = Queue()
        sse_queues.append(q)
        try:
            yield f"data: {json.dumps({'type': 'hello', 'payload': {}, 'ts': now_iso()})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {json.dumps(msg)}\n\n"
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
    threading.Thread(target=mqtt_loop,       daemon=True).start()
    threading.Thread(target=keepalive_timer, daemon=True).start()
    threading.Thread(target=auto_alarm_timer, daemon=True).start()
    app.run(host="0.0.0.0", port=8090, threaded=True)
