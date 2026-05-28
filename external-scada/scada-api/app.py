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
import binascii
import json
import os
import random
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from queue import Queue, Empty

import paho.mqtt.client as mqtt
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Flask, Response, jsonify, request, send_from_directory


# ── Config (env + runtime-mutable) ─────────────────────────────────────
MQTT_HOST  = os.getenv("MQTT_HOST",  "10.4.0.10")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER  = os.getenv("MQTT_USER",  "admin")
MQTT_PASS  = os.getenv("MQTT_PASS",  "admin")

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

AES_KEY_B64 = os.getenv("SCADA_AES_KEY", "k7Qh2NfT8vR0mC9aXy4pLwZbE3sG6uJtH1iKd5oArMw=")
AES_KEY = base64.b64decode(AES_KEY_B64) if AES_KEY_B64 else None

# Runtime-mutable config — EVERY value below is editable live via
# POST /api/config and takes effect immediately. Nothing is a restart
# requirement. Dashboard reflects changes via SSE.
def _envbool(key: str, default: str) -> bool:
    return os.getenv(key, default).strip().lower() == "true"

runtime = {
    # ── Timer intervals ─────────────────────────
    # SILENT BY DEFAULT: a fresh deploy publishes NOTHING until enabled,
    # either from the dashboard ("Start"/"Start all") or via env overrides.
    # To auto-start publishing on boot, set the *_ENABLED env vars to "true"
    # (and KEEPALIVE_PAUSED to "false"). Dashboard changes take effect live
    # but do NOT persist across restarts — the env vars are the durable knob.
    "keepalive_seconds":        int(os.getenv("KEEPALIVE_SECONDS", "30")),
    "auto_alarm_seconds":       int(os.getenv("AUTO_ALARM_SECONDS", "10")),     # 0 = disabled
    "auto_alarm_enabled":       _envbool("AUTO_ALARM_ENABLED", "false"),
    "keepalive_paused":         _envbool("KEEPALIVE_PAUSED", "true"),
    "send_all_alarms_seconds":  int(os.getenv("SEND_ALL_ALARMS_SECONDS", "60")),
    "send_all_alarms_enabled":  _envbool("SEND_ALL_ALARMS_ENABLED", "false"),
    "get_all_alarms_seconds":   int(os.getenv("GET_ALL_ALARMS_SECONDS", "120")),
    "get_all_alarms_enabled":   _envbool("GET_ALL_ALARMS_ENABLED", "false"),

    # ── Identity & topics (mutable) ─────────────
    "creator_id":               CREATOR_ID,
    "topic_out":                TOPIC_OUT,
    "topic_in":                 TOPIC_IN,

    # ── Equipment list (mutable via /api/config) ─
    "equipment":                [],     # filled below after class defs

    # ── Runtime status (read-only from dashboard) ─
    "mqtt_connected":           False,
    "last_keepalive_sent":      None,
    "last_tms_received":        None,
    "last_alarm_sent":          None,
    "last_send_all_sent":       None,
    "last_get_all_sent":        None,
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
    "sent_get_all": 0,
    "decrypt_ok":   0,
    "decrypt_fail": 0,
    "dedup_skipped": 0,
}

# Last known state per alarm Id — used to drop unchanged repeats from
# the inbound MQTT stream (flood-control: snapshot replays after a
# reconnect can re-send the same state SCADA already has).
# value: (state, timestamp_str)
last_seen_alarm: dict[str, tuple[str, str]] = {}

sse_queues: list[Queue] = []

# Default equipment list — used to seed runtime["equipment"]
# Fully mutable via POST /api/config {"equipment": [...]}
DEFAULT_EQUIPMENT = [
    "ESC_ROL_1_23", "ESC_ROL_2_27", "ESC_ROL_3_15",
    "DOOR_PLT_2_3", "DOOR_PLT_1_1", "DOOR_STN_1_MAIN",
    "ELV_STN_4_01", "ELV_STN_2_03",
    "DNVEL01.ALC.ED06", "DNVEL02.ALC.ED06",
    "PUMP_DRY_1", "PUMP_DRY_2",
    "HVAC_PLT_1", "FIRE_DETECT_OFFICE_A",
    "CCTV_PLT_2_CAM5",
]
runtime["equipment"] = list(DEFAULT_EQUIPMENT)

# Rolling-window rate tracking (60-sec window for msgs/sec calc)
_rate_window_in  = deque(maxlen=600)   # timestamps of received msgs
_rate_window_out = deque(maxlen=600)   # timestamps of sent msgs

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


def encrypt_payload(plain_obj: dict) -> bytes:
    """AES-256-GCM encrypt + base64 encode — same wire format as decrypt_payload
    (12B IV + ciphertext + 16B GCM tag, base64 wrapped). Mirrors the bridge's
    EncryptProcessor so the reverse path can decrypt SCADA outbound."""
    plain = json.dumps(plain_obj).encode("utf-8")
    if AES_KEY is None:
        return plain
    iv = os.urandom(12)
    ciphertext = AESGCM(AES_KEY).encrypt(iv, plain, None)
    return base64.b64encode(iv + ciphertext)


def is_alarm_state_unchanged(alarm: dict) -> bool:
    """Return True if (Id, State) matches what we last saw with a non-newer
    timestamp. Used to silently drop replayed snapshot records that don't
    change state."""
    aid = alarm.get("Id")
    if not aid:
        return False
    state = str(alarm.get("State", ""))
    ts    = str(alarm.get("Timestamp", ""))
    seen  = last_seen_alarm.get(aid)
    if seen is None:
        return False
    seen_state, seen_ts = seen
    # Drop only when state is identical AND timestamp is not newer.
    return state == seen_state and ts <= seen_ts


def decrypt_payload(raw: bytes) -> dict:
    if AES_KEY is None:
        return {"raw_base64": base64.b64encode(raw).decode(),
                "note": "SCADA_AES_KEY not set — can't decrypt"}
    # Auto-detect base64-encoded payload (Connect pipeline) vs raw bytes
    # (legacy direct MQTT). Base64 contains only printable [A-Za-z0-9+/=].
    try:
        s = raw.decode("ascii")
        if all(c.isalnum() or c in "+/=\r\n" for c in s):
            raw = base64.b64decode(s, validate=True)
    except (UnicodeDecodeError, ValueError, binascii.Error):
        pass  # not base64, treat as raw bytes
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
    _rate_window_in.append(time.time())
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

        # Flood-control dedup: if this is a replayed UpdateAlarm whose
        # state matches what we've already seen, drop silently.
        alarm = None
        if isinstance(decoded, dict):
            if decoded.get("Type") == "UpdateAlarm":
                alarm = decoded.get("Alarm")
            elif "envelope" in decoded:
                env = decoded.get("envelope") or {}
                if env.get("Type") == "UpdateAlarm":
                    alarm = env.get("Alarm")
        if alarm and is_alarm_state_unchanged(alarm):
            stats["dedup_skipped"] += 1
            entry["dedup_skipped"] = True
            return  # do not store, do not broadcast
        if alarm and alarm.get("Id"):
            last_seen_alarm[alarm["Id"]] = (
                str(alarm.get("State", "")),
                str(alarm.get("Timestamp", "")),
            )

        # If TMS just sent us a GetAllAlarms, respond with SendAllAlarms —
        # but only when SendAllAlarms is enabled, so an "off" SCADA stays
        # silent even when TMS keeps polling with GetAllAlarms.
        if (isinstance(decoded, dict) and decoded.get("Type") == "GetAllAlarms"
                and runtime["send_all_alarms_enabled"]):
            send_all_alarms()
    except Exception as e:
        entry["error"]      = str(e)
        entry["raw_base64"] = base64.b64encode(msg.payload).decode()
        stats["decrypt_fail"] += 1

    messages_in.appendleft(entry)
    broadcast_sse("received", entry)
    print(f"[scada-api] RX {msg.topic} ({len(msg.payload)}b): {entry.get('decoded') or entry.get('error')}", flush=True)


# ── SCADA publisher — the "sending" side ─────────────────────────────
# Outbound (SCADA→TMS) goes plain JSON when SCADA_OUTBOUND_ENCRYPT is
# unset/false. The bridge's bridge.reverse-kafka.encrypt-enabled=false
# matches this so the reverse path passes the bytes through unchanged.
SCADA_OUTBOUND_ENCRYPT = os.getenv("SCADA_OUTBOUND_ENCRYPT", "false").lower() == "true"

def publish(topic: str, envelope: dict, kind: str):
    if mqtt_client is None or not runtime["mqtt_connected"]:
        return False
    if SCADA_OUTBOUND_ENCRYPT:
        payload = encrypt_payload(envelope)
    else:
        payload = json.dumps(envelope).encode("utf-8")
    mqtt_client.publish(topic, payload, qos=0)
    _rate_window_out.append(time.time())
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
        "CreatorId": runtime["creator_id"],
        "Type":      "UpdateAlarm",
        "Timestamp": now_rsae(),
        "Alarm":     alarm,
    }
    alarm_db[alarm_id] = alarm
    if publish(runtime["topic_out"], envelope, "UpdateAlarm"):
        stats["sent_alarm"] += 1
        runtime["last_alarm_sent"] = now_iso()
        return True
    return False


def publish_keepalive():
    if runtime["keepalive_paused"]:
        return False
    envelope = {
        "CreatorId": runtime["creator_id"],
        "Type":      "KeepAlive",
        "Timestamp": now_rsae(),
    }
    ok = publish(runtime["topic_out"], envelope, "KeepAlive")
    if ok:
        stats["sent_ka"] += 1
        runtime["last_keepalive_sent"] = now_iso()
        keepalives.appendleft({"sentAt": now_iso(), "envelope": envelope})
    return ok


def send_all_alarms():
    envelope = {
        "CreatorId": runtime["creator_id"],
        "Type":      "SendAllAlarms",
        "Timestamp": now_rsae(),
        "Alarms":    list(alarm_db.values()),
    }
    ok = publish(runtime["topic_out"], envelope, "SendAllAlarms")
    if ok:
        stats["sent_all"] += 1
        runtime["last_send_all_sent"] = now_iso()
    return ok


def publish_get_all_alarms():
    """SCADA proactively requests full alarm state from TMS."""
    envelope = {
        "CreatorId": runtime["creator_id"],
        "Type":      "GetAllAlarms",
        "Timestamp": now_rsae(),
    }
    ok = publish(runtime["topic_out"], envelope, "GetAllAlarms")
    if ok:
        stats["sent_get_all"] += 1
        runtime["last_get_all_sent"] = now_iso()
    return ok


def compute_rate(window: "deque") -> float:
    """Messages per second over the last 60s, live computation."""
    now = time.time()
    recent = [t for t in window if now - t <= 60.0]
    return round(len(recent) / 60.0, 2)


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
                runtime["auto_alarm_seconds"] <= 0 or
                not runtime["equipment"]):
            continue
        # Sleep rest of the interval (picks up runtime changes on next tick)
        time.sleep(max(0, runtime["auto_alarm_seconds"] - 1))
        if (runtime["auto_alarm_enabled"] and
                runtime["mqtt_connected"] and
                runtime["equipment"]):
            eq = random.choice(runtime["equipment"])
            state = random.choice(["0", "1", "0", "0"])  # skewed toward OK
            publish_update_alarm(eq, state)


def send_all_alarms_timer():
    while True:
        time.sleep(1)
        if (not runtime["send_all_alarms_enabled"] or
                not runtime["mqtt_connected"] or
                runtime["send_all_alarms_seconds"] <= 0):
            continue
        time.sleep(max(0, runtime["send_all_alarms_seconds"] - 1))
        if (runtime["send_all_alarms_enabled"] and
                runtime["mqtt_connected"] and
                runtime["send_all_alarms_seconds"] > 0):
            send_all_alarms()


def get_all_alarms_timer():
    while True:
        time.sleep(1)
        if (not runtime["get_all_alarms_enabled"] or
                not runtime["mqtt_connected"] or
                runtime["get_all_alarms_seconds"] <= 0):
            continue
        time.sleep(max(0, runtime["get_all_alarms_seconds"] - 1))
        if (runtime["get_all_alarms_enabled"] and
                runtime["mqtt_connected"] and
                runtime["get_all_alarms_seconds"] > 0):
            publish_get_all_alarms()


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
    # Disable HTTP caching for the dashboard shell so iterative UI
    # changes during a demo always render on next reload — Flask's
    # default static cache headers are aggressive (12-hour max-age)
    # which masks redeploys until the user opens DevTools.
    resp = send_from_directory("static", "dashboard.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/config")
def config_page():
    """Dedicated config & scenario editor screen — separate from /."""
    resp = send_from_directory("static", "config.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/status")
def api_status():
    return jsonify({
        "creatorId":  runtime["creator_id"],
        "mqtt": {
            "host":     MQTT_HOST,
            "port":     MQTT_PORT,
            "topicIn":  runtime["topic_in"],
            "topicOut": runtime["topic_out"],
            "connected": runtime["mqtt_connected"],
        },
        "runtime":    runtime,
        "stats":      stats,
        "alarmCount": len(alarm_db),
    })


# ── TMS Publisher (for demo: synthesize TMS-side messages and POST them
# to the bridge's REST publish endpoint, which writes to Artemis) ──────
BRIDGE_URL = os.getenv("BRIDGE_URL",
            "http://pas-scada-bridge.pinkline.svc.cluster.local:8085").rstrip("/")

tms_pub = {
    "enabled":    False,
    "interval_s": 5,
    "topic":      "TMS.PISInfo",
    "sent":       0,
    "errors":     0,
    "last_at":    None,
    "last_error": None,
}
TMS_TOPICS = [
    "jms.topic.TMS.PISInfo",
    "jms.topic.RCS.E2K.TMS.TrafficReportClient",
    "jms.topic.TSInfo",
    "jms.topic.RCS.E2K.TMS.RouteInfo",
    "jms.topic.rcs.e2k.ctc.train.pas",
]

def _short_topic(t: str) -> str:
    """Strip jms.topic. prefix to look up the right XML template generator."""
    return t.split(".", 2)[2] if t.startswith("jms.topic.") else t

def _tms_xml(topic: str) -> str:
    """Generate REALISTIC XML matching the actual TMS schema each topic
    uses, so the bridge's XmlToJsonProcessor produces meaningful JSON
    that matches the production wire format.

    Topic name may be the full `jms.topic.X` (preferred — matches bridge
    allow-list) or just `X`; both are handled.

    Schemas come from the Java POJOs in
    tms/src/main/java/com/pinkline/kafkabridge/model/."""
    topic = _short_topic(topic)
    now = datetime.now(timezone.utc)
    ts_compact   = now.strftime("%Y%m%dT%H%M%S")             # 20260430T143012
    ts_iso       = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")    # ISO8601
    seconds_today = now.hour * 3600 + now.minute * 60 + now.second
    train_id  = f"T{random.randint(1000, 9999)}"
    tt_guid   = f"{random.randint(100000, 999999)}-{random.randint(1000, 9999)}-{random.randint(100, 999)}"
    trip_no   = random.randint(100, 999)
    line_id   = random.randint(101, 109)
    platform  = random.randint(2200, 2299)
    arrival   = (seconds_today + 60) % 86400
    departure = arrival + 60

    # ── TMS.PISInfo: Passenger Info platform/train info (rcs.e2k.ctc.train.pas.V1) ──
    if topic == "TMS.PISInfo":
        station_id   = random.randint(10, 25)
        platform_id  = f"PL{station_id}{random.randint(0, 99):02d}"
        train_guid   = f"trafficscheduler-{random.randint(100000000, 999999999):09d}-" \
                       f"{random.randint(1000, 9999)}-{random.randint(1000, 9999)}-" \
                       f"{random.randint(100000000000, 999999999999)}"
        train_name   = f"{random.randint(1, 99):02d}"
        arrival_ts   = (now).strftime("%Y%m%dT%H%M%S")
        departure_ts = (now + timedelta(seconds=20)).strftime("%Y%m%dT%H%M%S")
        dest_id      = random.randint(2000, 2999)
        return (
            f'<rcsMsg>'
            f'<hdr>'
            f'<schema>rcs.e2k.ctc.train.pas.V1</schema>'
            f'<sender>RCS.E2K.PIS</sender>'
            f'</hdr>'
            f'<data>'
            f'<PlatformInfos>'
            f'<PlatformInfo platform="{platform_id}">'
            f'<StationId>{station_id}</StationId>'
            f'<NextTrainApproacing>no</NextTrainApproacing>'
            f'<Trains>'
            f'<Train idx="0" id="0" GUID="{train_guid}" name="{train_name}" tripno="{trip_no}">'
            f'<WillStop>yes</WillStop>'
            f'<AtStation>no</AtStation>'
            f'<LastForToday>no</LastForToday>'
            f'<ArrivalTime>{arrival_ts}</ArrivalTime>'
            f'<DepartureTime>{departure_ts}</DepartureTime>'
            f'<DestinationId>{dest_id}</DestinationId>'
            f'</Train>'
            f'</Trains>'
            f'</PlatformInfo>'
            f'</PlatformInfos>'
            f'</data>'
            f'</rcsMsg>')

    # ── RCS.E2K.TMS.TrafficReportClient: alternates Arrival / Departure ──
    if topic == "RCS.E2K.TMS.TrafficReportClient":
        if random.random() < 0.5:
            return (
                f'<SingleArrival>'
                f'<Arr>'
                f'<train>'
                f'<TTGUID>{tt_guid}</TTGUID>'
                f'<id>{train_id}</id>'
                f'</train>'
                f'<loc><tmsid>{platform}</tmsid></loc>'
                f'<atimes><oTime>{ts_iso}</oTime></atimes>'
                f'</Arr>'
                f'</SingleArrival>')
        return (
            f'<SingleDeparture>'
            f'<Dep>'
            f'<train>'
            f'<TTGUID>{tt_guid}</TTGUID>'
            f'<id>{train_id}</id>'
            f'</train>'
            f'<loc><tmsid>{platform}</tmsid></loc>'
            f'<dtimes><oTime>{ts_iso}</oTime></dtimes>'
            f'</Dep>'
            f'</SingleDeparture>')

    # ── TSInfo: track section / signalling status (no Java POJO yet —
    #          treated as generic XML and converted via Jackson XmlMapper) ──
    if topic == "TSInfo":
        section_id = f"TS_{platform}_{random.randint(1, 9)}"
        state = random.choice(["OCCUPIED", "CLEAR", "FAULT", "RESERVED"])
        return (
            f'<TSInfo>'
            f'<dateTime>{ts_compact}</dateTime>'
            f'<Section id="{section_id}" state="{state}" '
            f'lineId="{line_id}" trainId="{train_id}"/>'
            f'</TSInfo>')

    # ── RCS.E2K.TMS.RouteInfo: route destinations for a train ──
    return (
        f'<routeinfo>'
        f'<TTGUID>{tt_guid}</TTGUID>'
        f'<trainid>{train_id}</trainid>'
        f'<dests>'
        f'<dest tmsid="{platform}"/>'
        f'<dest tmsid="{platform + 1}"/>'
        f'<dest tmsid="{platform + 2}"/>'
        f'</dests>'
        f'</routeinfo>')


def _tms_publish(topic: str, xml: str) -> None:
    url = f"{BRIDGE_URL}/api/tms-publish/{topic}"
    r = requests.post(url, data=xml.encode("utf-8"),
                      headers={"Content-Type": "application/xml"},
                      timeout=5)
    r.raise_for_status()
    tms_pub["sent"] += 1
    tms_pub["last_at"] = now_iso()
    tms_pub["last_error"] = None


def _tms_publish_loop():
    while True:
        if tms_pub["enabled"]:
            try:
                _tms_publish(tms_pub["topic"], _tms_xml(tms_pub["topic"]))
            except Exception as e:
                tms_pub["errors"] += 1
                tms_pub["last_error"] = f"{type(e).__name__}: {e}"
            time.sleep(max(1, int(tms_pub["interval_s"])))
        else:
            time.sleep(0.5)


threading.Thread(target=_tms_publish_loop, daemon=True, name="tms-publisher").start()


@app.route("/api/tms-publish", methods=["POST"])
def api_tms_publish_now():
    """Manual one-shot publish. Body: {"topic": "...", "xml": "..."}.
    Both fields optional — defaults to current toggle topic + synthesized XML.
    Topic is free-form (UI lets users type custom topics for testing)."""
    body = request.get_json(silent=True) or {}
    topic = body.get("topic") or tms_pub["topic"]
    xml = body.get("xml") or _tms_xml(topic if topic in TMS_TOPICS else "TMS.PISInfo")
    try:
        _tms_publish(topic, xml)
        return jsonify({"ok": True, "topic": topic, "sent": tms_pub["sent"]})
    except Exception as e:
        tms_pub["errors"] += 1
        tms_pub["last_error"] = f"{type(e).__name__}: {e}"
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/tms-publish/config", methods=["POST"])
def api_tms_publish_config():
    """Update auto-publisher config. Body keys: enabled, interval_s, topic."""
    body = request.get_json(silent=True) or {}
    if "enabled" in body:
        tms_pub["enabled"] = bool(body["enabled"])
    if "interval_s" in body:
        try: tms_pub["interval_s"] = max(1, int(body["interval_s"]))
        except Exception: pass
    if "topic" in body and body["topic"]:
        tms_pub["topic"] = body["topic"]   # free-form, accept any topic name
    return jsonify(tms_pub)


@app.route("/api/tms-publish/state")
def api_tms_publish_state():
    return jsonify({**tms_pub, "topics": TMS_TOPICS, "bridge_url": BRIDGE_URL})


@app.route("/api/tms-publish/template")
def api_tms_publish_template():
    """Return an auto-generated XML template for a topic — used by the config
    editor to pre-fill the textarea, then user edits + sends scenarios."""
    topic = request.args.get("topic", tms_pub["topic"])
    # Free-form: fall back to PISInfo template for unknown topics (UI may type any topic)
    template_topic = topic if topic in TMS_TOPICS else "TMS.PISInfo"
    try:
        return jsonify({"ok": True, "topic": topic, "xml": _tms_xml(template_topic)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/scada-envelope/template")
def api_scada_envelope_template():
    """Auto-generated RSAE envelope template for the config editor."""
    kind = request.args.get("kind", "UpdateAlarm")
    creator = runtime["creator_id"]
    ts = now_rsae()
    if kind == "UpdateAlarm":
        env = {"CreatorId": creator, "Type": "UpdateAlarm", "Timestamp": ts,
               "Alarm": {"Timestamp": ts, "Id": "ESC_ROL_1_23", "State": "1"}}
    elif kind == "KeepAlive":
        env = {"CreatorId": creator, "Type": "KeepAlive", "Timestamp": ts}
    elif kind == "SendAllAlarms":
        env = {"CreatorId": creator, "Type": "SendAllAlarms", "Timestamp": ts,
               "Alarms": list(alarm_db.values())}
    elif kind == "GetAllAlarms":
        env = {"CreatorId": creator, "Type": "GetAllAlarms", "Timestamp": ts}
    else:
        return jsonify({"ok": False, "error": "unknown kind"}), 400
    return jsonify({"ok": True, "kind": kind, "envelope": env})


@app.route("/api/scada-publish-raw", methods=["POST"])
def api_scada_publish_raw():
    """Publish a raw RSAE envelope (JSON) to topic_out — for scenario testing.
    Body: {"kind": "UpdateAlarm|KeepAlive|SendAllAlarms|GetAllAlarms",
           "envelope": {...}}
    Bypasses auto-building so the user can craft arbitrary payloads."""
    body = request.get_json(silent=True) or {}
    kind     = body.get("kind", "UpdateAlarm")
    envelope = body.get("envelope")
    if not isinstance(envelope, dict):
        return jsonify({"ok": False, "error": "envelope must be a JSON object"}), 400
    try:
        ok = publish(runtime["topic_out"], envelope, kind)
        if ok:
            if   kind == "UpdateAlarm":   stats["sent_alarm"]  += 1
            elif kind == "KeepAlive":     stats["sent_ka"]     += 1
            elif kind == "SendAllAlarms": stats["sent_all"]    += 1
            elif kind == "GetAllAlarms":  stats["sent_get_all"] += 1
        return jsonify({"ok": ok, "kind": kind, "topic": runtime["topic_out"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/power-scada/template")
def api_power_scada_template():
    """Auto-generated power-segment JSON template for the config editor."""
    mode = request.args.get("mode", power_scada_pub["mode"])
    if mode not in POWER_MODES:
        return jsonify({"ok": False, "error": "unknown mode", "allowed": list(POWER_MODES)}), 400
    try:
        return jsonify({"ok": True, "mode": mode, "payload": _power_scada_payload(mode)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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


@app.route("/api/metrics")
def api_metrics():
    """Live throughput + latency metrics for the flow diagram."""
    last_in  = runtime.get("last_tms_received")
    last_out = runtime.get("last_keepalive_sent")
    silence_in  = (datetime.now(timezone.utc) - datetime.fromisoformat(last_in)).total_seconds() if last_in else None
    silence_out = (datetime.now(timezone.utc) - datetime.fromisoformat(last_out)).total_seconds() if last_out else None
    return jsonify({
        "rate_in_per_sec":   compute_rate(_rate_window_in),
        "rate_out_per_sec":  compute_rate(_rate_window_out),
        "silence_in_sec":    silence_in,
        "silence_out_sec":   silence_out,
        "total_received":    stats["received"],
        "total_sent":        stats["sent_alarm"] + stats["sent_ka"] + stats["sent_all"] + stats["sent_get_all"],
        "mqtt_connected":    runtime["mqtt_connected"],
    })


@app.route("/api/config", methods=["POST"])
def api_config():
    """Every runtime value configurable from dashboard. No restart needed."""
    body = request.get_json(force=True, silent=True) or {}

    # Numeric intervals
    for key in ("keepalive_seconds", "auto_alarm_seconds",
                "send_all_alarms_seconds", "get_all_alarms_seconds"):
        if key in body:
            try:
                runtime[key] = max(0, int(body[key]))
            except Exception:
                pass

    # Booleans
    for key in ("auto_alarm_enabled", "keepalive_paused",
                "send_all_alarms_enabled", "get_all_alarms_enabled"):
        if key in body:
            runtime[key] = bool(body[key])

    # Strings (identity + topics)
    for key in ("creator_id", "topic_out"):
        if key in body and isinstance(body[key], str) and body[key].strip():
            runtime[key] = body[key].strip()

    # topic_in change requires MQTT re-subscribe
    if "topic_in" in body and isinstance(body["topic_in"], str) and body["topic_in"].strip():
        old = runtime["topic_in"]
        new = body["topic_in"].strip()
        if new != old and mqtt_client and runtime["mqtt_connected"]:
            try:
                mqtt_client.unsubscribe(old)
                mqtt_client.subscribe(new)
                print(f"[scada-api] re-subscribed {old} → {new}", flush=True)
            except Exception as e:
                print(f"[scada-api] topic_in swap failed: {e}", flush=True)
        runtime["topic_in"] = new

    # Equipment list
    if "equipment" in body and isinstance(body["equipment"], list):
        runtime["equipment"] = [str(x).strip() for x in body["equipment"] if str(x).strip()]

    broadcast_sse("config", runtime)
    return jsonify(runtime)


@app.route("/api/publish", methods=["POST"])
def api_publish():
    body = request.get_json(force=True, silent=True) or {}
    alarm_id = body.get("id") or random.choice(DEFAULT_EQUIPMENT)
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


@app.route("/api/getall", methods=["POST"])
def api_getall():
    ok = publish_get_all_alarms()
    return jsonify({"ok": ok})


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


# ══════════════════════════════════════════════════════════════════════
# Power SCADA publisher — pushes JSON payloads to Artemis broker on
# topic POWER_SCADA_TMS_INFO. The downstream C++ Power Dashboard app
# subscribes via STOMP and renders the segment power states.
# Modes:
#   random  — every segment gets a random status 0..3 (debug feed)
#   all_on  — every segment energized (powerStatus=1)  → all rails green
#   all_off — every segment de-energized (powerStatus=2) → all rails red
#   mixed   — 70% energized / 20% no power / 10% error (realistic)
# ══════════════════════════════════════════════════════════════════════
ARTEMIS_HOST  = os.getenv("ARTEMIS_HOST",  "host.docker.internal")
ARTEMIS_PORT  = int(os.getenv("ARTEMIS_PORT", "61616"))
ARTEMIS_USER  = os.getenv("ARTEMIS_USER",  "admin")
ARTEMIS_PASS  = os.getenv("ARTEMIS_PASS",  "admin")
ARTEMIS_TOPIC = os.getenv("ARTEMIS_TOPIC", "POWER_SCADA_TMS_INFO")

# Track layout for the visual dashboard — each row is one line, each entry a
# power segment. Names mirror the client's reference schematic so the web
# dashboard (/power) renders the same topology the demo expects.
POWER_LINES = [
    ["P.CHA", "BAMBU", "CHAM",  "P.CAS"],
    ["VALDE", "TETUA", "N04",   "C.C.",  "N01"],
    ["R.ROS", "IGLES", "BILBA", "TRIBU", "G.VIA"],
    ["SOL",   "T.MOL", "A.MAR", "ATOCH", "A.REN", "M.PEL"],
    ["PACIF", "VALLE", "N.NUM", "PORTA", "B.AIR"],
    ["A.ARE", "M.HER", "S.GUA", "V.VAL", "CONGO"],
    ["PROG",  "N26",   "GAVIA", "SUERT", "CARRO"],
    ["E.A.1", "N24",   "N25",   "C12",   "AS"],
    ["N29",   "N27",   "N22",   "VA",    "N21"],
]
POWER_SEGMENT_IDS = [sid for line in POWER_LINES for sid in line]
POWER_MODES = ("random", "all_on", "all_off", "mixed")

power_scada_pub = {
    "enabled":    False,
    "interval_s": 5,
    "mode":       "mixed",
    "sent":       0,
    "errors":     0,
    "last_at":    None,
    "last_error": None,
}

# Latest power payload generated — served to /power for instant first paint.
_power_last = {"payload": None}


def _power_scada_payload(mode: str) -> dict:
    """Build a fresh Power SCADA JSON payload for `mode`."""
    now = datetime.now(timezone.utc)
    seq = power_scada_pub["sent"] + 1

    if mode == "all_on":
        statuses = {sid: 1 for sid in POWER_SEGMENT_IDS}
    elif mode == "all_off":
        statuses = {sid: 2 for sid in POWER_SEGMENT_IDS}
    elif mode == "mixed":
        statuses = {}
        for sid in POWER_SEGMENT_IDS:
            r = random.random()
            statuses[sid] = 1 if r < 0.70 else (2 if r < 0.90 else 3)
    else:  # random
        statuses = {sid: random.randint(0, 3) for sid in POWER_SEGMENT_IDS}

    return {
        "metadata": {
            "messageType":   "POWER_SCADA_TMS_INFO",
            "schemaVersion": "1.0",
            "timestamp":     now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        "header": {
            "SourceId":  "SCADA",
            "health":    1,
            "healthSeq": seq,
        },
        "powerSegmentsStatus": [
            {"segmentID": sid, "powerStatus": st}
            for sid, st in statuses.items()
        ],
    }


def _power_scada_publish_json(payload: dict) -> None:
    """Send a JSON payload to Artemis via STOMP over a single short
    connection. Raises on any failure."""
    body = json.dumps(payload)
    sock = socket.create_connection((ARTEMIS_HOST, ARTEMIS_PORT), timeout=5)
    try:
        sock.settimeout(5)
        connect_frame = (
            "CONNECT\n"
            "accept-version:1.2\n"
            f"host:{ARTEMIS_HOST}\n"
            f"login:{ARTEMIS_USER}\n"
            f"passcode:{ARTEMIS_PASS}\n"
            "heart-beat:0,0\n"
            "\n\x00"
        )
        sock.sendall(connect_frame.encode("utf-8"))

        resp = b""
        while b"\x00" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("Artemis closed socket during CONNECT")
            resp += chunk
        if b"CONNECTED" not in resp.split(b"\x00", 1)[0]:
            raise RuntimeError(f"STOMP CONNECT rejected: {resp[:200]!r}")

        send_frame = (
            "SEND\n"
            f"destination:{ARTEMIS_TOPIC}\n"
            "content-type:application/json\n"
            "\n"
            f"{body}\x00"
        )
        sock.sendall(send_frame.encode("utf-8"))
        try:
            sock.sendall(b"DISCONNECT\n\n\x00")
        except OSError:
            pass
    finally:
        sock.close()


def _power_scada_record_ok():
    power_scada_pub["sent"] += 1
    power_scada_pub["last_at"] = now_iso()
    power_scada_pub["last_error"] = None


def _power_scada_record_err(e: Exception):
    power_scada_pub["errors"] += 1
    power_scada_pub["last_error"] = f"{type(e).__name__}: {e}"


def _power_scada_loop():
    while True:
        if power_scada_pub["enabled"]:
            payload = _power_scada_payload(power_scada_pub["mode"])
            # Always feed the live web dashboard (/power), independent of
            # whether the Artemis STOMP publish succeeds — so the visual
            # demo keeps running even if the broker is unreachable.
            _power_last["payload"] = payload
            broadcast_sse("power", payload)
            try:
                _power_scada_publish_json(payload)
                _power_scada_record_ok()
            except Exception as e:
                _power_scada_record_err(e)
            time.sleep(max(1, int(power_scada_pub["interval_s"])))
        else:
            time.sleep(0.5)


@app.route("/api/power-scada/publish", methods=["POST"])
def api_power_scada_publish_now():
    """Manual one-shot publish to Artemis.
    Body: {"mode": "...", "payload": {...}} — both optional."""
    body = request.get_json(silent=True) or {}
    mode = body.get("mode") or power_scada_pub["mode"]
    if mode not in POWER_MODES:
        return jsonify({"ok": False, "error": "unknown mode", "allowed": list(POWER_MODES)}), 400
    payload = body.get("payload") or _power_scada_payload(mode)
    _power_last["payload"] = payload
    broadcast_sse("power", payload)
    try:
        _power_scada_publish_json(payload)
        _power_scada_record_ok()
        return jsonify({"ok": True, "sent": power_scada_pub["sent"], "mode": mode, "payload": payload})
    except Exception as e:
        _power_scada_record_err(e)
        # Artemis may be unreachable, but the web dashboard still gets the
        # payload in the response + via SSE — so /power stays live.
        return jsonify({"ok": False, "error": str(e), "payload": payload}), 200


@app.route("/api/power-scada/config", methods=["POST"])
def api_power_scada_config():
    """Update auto-publisher config. Body keys: enabled, interval_s, mode."""
    body = request.get_json(silent=True) or {}
    if "enabled" in body:
        power_scada_pub["enabled"] = bool(body["enabled"])
    if "interval_s" in body:
        try:
            power_scada_pub["interval_s"] = max(1, int(body["interval_s"]))
        except Exception:
            pass
    if "mode" in body and body["mode"] in POWER_MODES:
        power_scada_pub["mode"] = body["mode"]
    return jsonify(power_scada_pub)


@app.route("/api/power-scada/state")
def api_power_scada_state():
    return jsonify({
        **power_scada_pub,
        "topic":   ARTEMIS_TOPIC,
        "broker":  f"{ARTEMIS_HOST}:{ARTEMIS_PORT}",
        "modes":   list(POWER_MODES),
        "segments_total": len(POWER_SEGMENT_IDS),
    })


@app.route("/api/power-layout")
def api_power_layout():
    """Track topology for the visual Power dashboard (/power)."""
    return jsonify({"lines": POWER_LINES})


@app.route("/api/power-scada/last")
def api_power_scada_last():
    """Most recent power payload — lets /power paint colours instantly on
    load instead of waiting for the next SSE tick."""
    return jsonify(_power_last["payload"] or _power_scada_payload(power_scada_pub["mode"]))


@app.route("/power")
def power_dashboard():
    """Clean, presentation-grade Power Segment track view. Renders the
    POWER_LINES topology and colours each segment's rails by powerStatus,
    fed live from the SSE 'power' events."""
    resp = send_from_directory("static", "power.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


if __name__ == "__main__":
    threading.Thread(target=mqtt_loop,             daemon=True).start()
    threading.Thread(target=keepalive_timer,       daemon=True).start()
    threading.Thread(target=auto_alarm_timer,      daemon=True).start()
    threading.Thread(target=send_all_alarms_timer, daemon=True).start()
    threading.Thread(target=get_all_alarms_timer,  daemon=True).start()
    threading.Thread(target=_power_scada_loop,     daemon=True, name="power-scada-publisher").start()
    app.run(host="0.0.0.0", port=8091, threaded=True)
