#!/usr/bin/env python3
# PAS-SCADA Health Monitor
# Probes every component in the bridge stack and emails on failure.
# State persisted to disk so restarts don't cause alert spam.
#
# HTTP layer: FastAPI + uvicorn (port 8080)
#   GET /healthz   liveness probe (always 200 once the app is up)
#   GET /readyz    readiness probe
#   GET /state     full probe state as JSON
#   GET /docs      Swagger UI for the above (FastAPI's built-in)

import json
import logging
import os
import smtplib
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import html
import requests
import uvicorn
import yaml
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse

CONFIG_PATH = os.getenv("MONITOR_CONFIG", "/etc/monitor/config.yaml")
STATE_PATH  = Path(os.getenv("MONITOR_STATE", "/var/state/state.json"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM")
SMTP_TO   = [r.strip() for r in os.getenv("SMTP_TO", "").split(",") if r.strip()]
SMTP_TLS  = os.getenv("SMTP_TLS", "true").lower() == "true"
SMTP_SSL  = os.getenv("SMTP_SSL", "false").lower() == "true"
ALERT_PREFIX = os.getenv("ALERT_PREFIX", "[PAS-SCADA]")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("monitor")


# ─── Time helpers ─────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_duration(start_iso: str, end_iso: str) -> str:
    secs = max(0, int((parse_iso(end_iso) - parse_iso(start_iso)).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h {rem // 60}m"


# ─── Probes ───────────────────────────────────────────────────────
# Each probe returns (ok: bool, message: str). Message is a short
# diagnostic that ends up in the alert email when it fails.

def _resolve_auth(check: dict):
    if check.get("auth") != "basic":
        return None
    user = os.getenv(check.get("username_env", ""), "")
    pwd  = os.getenv(check.get("password_env", ""), "")
    if not user:
        return None
    return (user, pwd)


def probe_http(check: dict):
    """Generic HTTP probe. Optional basic auth, status check, body match."""
    url     = check["url"]
    timeout = check.get("timeout_s", 5)
    expect  = check.get("expect_status", 200)
    contains = check.get("expect_body_contains")
    try:
        r = requests.get(url, timeout=timeout, auth=_resolve_auth(check), verify=False)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code != expect:
        return False, f"HTTP {r.status_code} (expected {expect})"
    if contains and contains not in r.text:
        return False, f"body missing '{contains}'"
    return True, f"HTTP {r.status_code}"


def probe_actuator(check: dict):
    """Spring Boot Actuator /actuator/health — reads JSON, checks status==UP.

    Reports which subcomponents are DOWN when the overall state is bad,
    so the alert email tells you *why* (e.g. kafka DOWN, rabbit UP).
    """
    url     = check["url"]
    timeout = check.get("timeout_s", 5)
    try:
        r = requests.get(url, timeout=timeout, auth=_resolve_auth(check))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    # 503 still has a valid body (Actuator returns DOWN with detail)
    if r.status_code not in (200, 503):
        return False, f"HTTP {r.status_code}"
    try:
        body = r.json()
    except Exception:
        return False, f"invalid JSON (HTTP {r.status_code})"
    status = body.get("status", "UNKNOWN")
    if status == "UP":
        return True, "UP"
    down = []
    for k, v in (body.get("components") or {}).items():
        sub = v.get("status") if isinstance(v, dict) else None
        if sub and sub != "UP":
            down.append(f"{k}={sub}")
    detail = ", ".join(down) if down else "no detail"
    return False, f"status={status} ({detail})"


def probe_tcp(check: dict):
    """TCP connect probe — for Kafka/Zookeeper/MQTT-only/AMQP-only."""
    host    = check["host"]
    port    = int(check["port"])
    timeout = check.get("timeout_s", 5)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"connected {host}:{port}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def probe_json_field(check: dict):
    """GET URL, parse JSON, assert a dotted path equals expected.

    Used for SCADA API: /api/status returns {"mqtt":{"connected":true}}.
    Set field=mqtt.connected, equals=true to alert when MQTT detaches.
    """
    url     = check["url"]
    timeout = check.get("timeout_s", 5)
    field   = check["field"]
    expect  = check["equals"]
    try:
        r = requests.get(url, timeout=timeout, auth=_resolve_auth(check))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        body = r.json()
    except Exception:
        return False, "invalid JSON"
    cursor = body
    for part in field.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return False, f"field '{field}' missing"
        cursor = cursor[part]
    if cursor != expect:
        return False, f"{field}={cursor!r} (expected {expect!r})"
    return True, f"{field}={cursor!r}"


PROBES = {
    "http":       probe_http,
    "actuator":   probe_actuator,
    "tcp":        probe_tcp,
    "json_field": probe_json_field,
}


# ─── State (consecutive fail/pass counters, persisted) ────────────

class State:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception as e:
            log.warning("state load failed (%s) — starting fresh", e)
            return {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def get(self, name: str) -> dict:
        with self.lock:
            return dict(self.data.get(name, {
                "consecutive_fails":  0,
                "consecutive_passes": 0,
                "current_state":      "UP",
                "last_state_change":  now_iso(),
                "last_ok_at":         None,
                "last_fail_at":       None,
                "last_error":         None,
            }))

    def update(self, name: str, fields: dict):
        with self.lock:
            current = self.data.setdefault(name, {})
            current.update(fields)
            self._save()

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))


# ─── Alerter (SMTP) ───────────────────────────────────────────────

class Alerter:
    def __init__(self):
        self.host = SMTP_HOST
        self.port = SMTP_PORT
        self.user = SMTP_USER
        self.password = SMTP_PASS
        self.sender = SMTP_FROM
        self.recipients = SMTP_TO
        self.tls = SMTP_TLS
        self.ssl = SMTP_SSL

    def configured(self) -> bool:
        return bool(self.host and self.recipients and self.sender)

    def send(self, subject: str, html: str, text: str):
        if not self.configured():
            log.warning("SMTP not configured — would send: %s", subject)
            return False
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        try:
            if self.ssl:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=15)
            with smtp:
                smtp.ehlo()
                if self.tls and not self.ssl:
                    smtp.starttls()
                    smtp.ehlo()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
            log.info("EMAIL sent: %s", subject)
            return True
        except Exception as e:
            log.error("EMAIL send failed (%s): %s", type(e).__name__, e)
            return False


# ─── Email templates ──────────────────────────────────────────────

DOWN_HTML = """<!doctype html>
<html><body style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0;padding:20px;background:#f7f7f7;color:#222;">
  <div style="background:#fff;border-left:5px solid #c0392b;padding:20px;border-radius:4px;">
    <h2 style="margin:0 0 12px 0;color:#c0392b;">Component DOWN: {name}</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr><td style="padding:6px 0;color:#777;width:160px;">Type</td><td>{check_type}</td></tr>
      <tr><td style="padding:6px 0;color:#777;">Endpoint</td><td><code>{endpoint}</code></td></tr>
      <tr><td style="padding:6px 0;color:#777;">First failed at</td><td>{first_fail}</td></tr>
      <tr><td style="padding:6px 0;color:#777;">Consecutive fails</td><td>{fails}</td></tr>
      <tr><td style="padding:6px 0;color:#777;">Last error</td><td><code style="color:#c0392b;">{error}</code></td></tr>
    </table>
    <h3 style="margin:20px 0 6px 0;font-size:14px;color:#555;">Other components currently DOWN</h3>
    <ul style="margin:0;padding-left:20px;font-size:13px;">{others_down}</ul>
  </div>
  <p style="color:#999;font-size:11px;margin-top:16px;">
    Sent by <b>pas-scada-monitor</b> at {now}. No further alerts for this component until it recovers.
  </p>
</body></html>
"""

DOWN_TEXT = """Component DOWN: {name}

Type:              {check_type}
Endpoint:          {endpoint}
First failed at:   {first_fail}
Consecutive fails: {fails}
Last error:        {error}

Other components currently DOWN: {others_down_text}

Sent by pas-scada-monitor at {now}.
No further alerts for this component until it recovers.
"""

RECOVERED_HTML = """<!doctype html>
<html><body style="font-family:-apple-system,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0;padding:20px;background:#f7f7f7;color:#222;">
  <div style="background:#fff;border-left:5px solid #27ae60;padding:20px;border-radius:4px;">
    <h2 style="margin:0 0 12px 0;color:#27ae60;">Component RECOVERED: {name}</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr><td style="padding:6px 0;color:#777;width:160px;">Endpoint</td><td><code>{endpoint}</code></td></tr>
      <tr><td style="padding:6px 0;color:#777;">Down since</td><td>{down_since}</td></tr>
      <tr><td style="padding:6px 0;color:#777;">Recovered at</td><td>{recovered_at}</td></tr>
      <tr><td style="padding:6px 0;color:#777;">Total downtime</td><td><b>{duration}</b></td></tr>
    </table>
  </div>
  <p style="color:#999;font-size:11px;margin-top:16px;">Sent by <b>pas-scada-monitor</b>.</p>
</body></html>
"""

RECOVERED_TEXT = """Component RECOVERED: {name}

Endpoint:        {endpoint}
Down since:      {down_since}
Recovered at:    {recovered_at}
Total downtime:  {duration}
"""


# ─── Engine ───────────────────────────────────────────────────────

def endpoint_label(check: dict) -> str:
    if "url" in check:
        return check["url"]
    if "host" in check and "port" in check:
        return f"{check['host']}:{check['port']}"
    return check.get("name", "?")


def others_down(state: State, exclude: str) -> list:
    snap = state.snapshot()
    return [name for name, s in snap.items()
            if name != exclude and s.get("current_state") == "DOWN"]


def send_down_alert(alerter: Alerter, check: dict, s: dict, state: State):
    others = others_down(state, check["name"])
    others_html = "<li>None</li>" if not others else "".join(
        f"<li><b>{n}</b></li>" for n in others)
    others_text = "none" if not others else ", ".join(others)
    ctx = {
        "name":         check["name"],
        "check_type":   check.get("type", "http"),
        "endpoint":     endpoint_label(check),
        "first_fail":   s.get("first_fail_at") or s.get("last_fail_at") or now_iso(),
        "fails":        s.get("consecutive_fails", 0),
        "error":        s.get("last_error", "") or "",
        "others_down":  others_html,
        "others_down_text": others_text,
        "now":          now_iso(),
    }
    subject = f"{ALERT_PREFIX} DOWN: {check['name']}"
    alerter.send(subject, DOWN_HTML.format(**ctx), DOWN_TEXT.format(**ctx))


def send_recovery_alert(alerter: Alerter, check: dict, s: dict):
    down_since = s.get("last_state_change", now_iso())
    recovered  = now_iso()
    duration   = fmt_duration(down_since, recovered)
    ctx = {
        "name":         check["name"],
        "endpoint":     endpoint_label(check),
        "down_since":   down_since,
        "recovered_at": recovered,
        "duration":     duration,
    }
    subject = f"{ALERT_PREFIX} RECOVERED: {check['name']} (down {duration})"
    alerter.send(subject, RECOVERED_HTML.format(**ctx), RECOVERED_TEXT.format(**ctx))


def evaluate(check: dict, ok: bool, msg: str, state: State, alerter: Alerter):
    name = check["name"]
    fail_th    = check.get("fail_threshold", 3)
    recover_th = check.get("recover_threshold", 1)

    s = state.get(name)
    new_state_change = None

    if ok:
        s["consecutive_fails"] = 0
        s["consecutive_passes"] = s.get("consecutive_passes", 0) + 1
        s["last_ok_at"] = now_iso()
        s["last_error"] = None
        s["first_fail_at"] = None
        if s["current_state"] == "DOWN" and s["consecutive_passes"] >= recover_th:
            send_recovery_alert(alerter, check, s)
            s["current_state"] = "UP"
            new_state_change = now_iso()
    else:
        # Capture *first* fail timestamp for the alert email — last_fail_at
        # updates every probe, but first_fail_at sticks until recovery.
        if s.get("consecutive_fails", 0) == 0:
            s["first_fail_at"] = now_iso()
        s["consecutive_passes"] = 0
        s["consecutive_fails"] = s.get("consecutive_fails", 0) + 1
        s["last_fail_at"] = now_iso()
        s["last_error"] = msg
        if s["current_state"] == "UP" and s["consecutive_fails"] >= fail_th:
            send_down_alert(alerter, check, s, state)
            s["current_state"] = "DOWN"
            new_state_change = now_iso()

    if new_state_change:
        s["last_state_change"] = new_state_change

    state.update(name, s)


def run_check(check: dict):
    probe_type = check.get("type", "http")
    fn = PROBES.get(probe_type)
    if fn is None:
        return False, f"unknown probe type: {probe_type}"
    try:
        return fn(check)
    except Exception as e:
        return False, f"probe crashed: {type(e).__name__}: {e}"


def probe_loop(state: State, alerter: Alerter, checks: list, interval: int,
               workers: int, stop: threading.Event):
    """Background thread: runs all probes every interval seconds until stop is set."""
    while not stop.is_set():
        cycle_start = time.time()
        log.debug("cycle start")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            futures = {pool.submit(run_check, c): c for c in checks}
            for fut in as_completed(futures):
                check = futures[fut]
                try:
                    ok, msg = fut.result()
                except Exception as e:
                    ok, msg = False, f"check crashed: {e}"
                level = logging.INFO if ok else logging.WARNING
                log.log(level, "[%s] %s — %s", "OK" if ok else "FAIL", check["name"], msg)
                evaluate(check, ok, msg, state, alerter)

        elapsed = time.time() - cycle_start
        sleep_for = max(1.0, interval - elapsed)
        # Sleep responsively so shutdown is fast.
        stop.wait(sleep_for)


# ─── FastAPI app ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load config, start probe loop in a background thread.
    Shutdown: signal the thread to stop."""
    cfg_path = Path(CONFIG_PATH)
    if not cfg_path.exists():
        log.error("config not found at %s", CONFIG_PATH)
        sys.exit(1)
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    interval = int(cfg.get("interval_s", 30))
    workers  = int(cfg.get("workers", 8))
    checks   = cfg.get("checks", [])
    if not checks:
        log.error("no checks configured")
        sys.exit(1)
    log.info("loaded %d checks, interval=%ds, workers=%d", len(checks), interval, workers)

    state = State(STATE_PATH)
    alerter = Alerter()
    if not alerter.configured():
        log.warning("SMTP not configured — alerts will be logged only "
                    "(set SMTP_HOST/SMTP_FROM/SMTP_TO env vars)")

    app.state.shared_state = state
    app.state.alerter = alerter
    app.state.stop_event = threading.Event()

    t = threading.Thread(
        target=probe_loop,
        args=(state, alerter, checks, interval, workers, app.state.stop_event),
        daemon=True,
        name="probe-loop",
    )
    t.start()
    log.info("probe loop started — every %ds", interval)

    yield

    log.info("shutdown — stopping probe loop")
    app.state.stop_event.set()


app = FastAPI(
    title="PAS-SCADA Health Monitor",
    description="Health probes for the PAS-SCADA pipeline. Emails on threshold cross.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/healthz", response_class=Response, summary="Liveness probe (k8s)")
def healthz():
    return Response("OK\n", media_type="text/plain")


@app.get("/readyz", response_class=Response, summary="Readiness probe (k8s)")
def readyz():
    return Response("OK\n", media_type="text/plain")


@app.get("/state", summary="Full probe state — current_state per check")
def state_endpoint():
    """Returns one entry per check with last_ok_at, last_fail_at, consecutive
    counters, current state UP|DOWN, etc. Useful for ad-hoc dashboards."""
    return JSONResponse(app.state.shared_state.snapshot())


# ─── Visual dashboard ─────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>PAS-SCADA · Mission Control</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@100;300;400;700;800&family=Major+Mono+Display&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg: #050807;
      --surface: #0a0e0d;
      --surface-2: rgba(17, 24, 20, 0.5);
      --line: #1a2522;
      --line-bright: #2c3a35;
      --ink: #d8e3df;
      --ink-dim: #5c6e67;
      --ink-mute: #344039;
      --up: #5cffaf;
      --up-glow: rgba(92, 255, 175, 0.18);
      --down: #ff4d57;
      --down-glow: rgba(255, 77, 87, 0.22);
      --warn: #ffd400;
      --line-dash: rgba(44, 58, 53, 0.5);
    }

    body {
      font-family: 'JetBrains Mono', ui-monospace, monospace;
      background: var(--bg);
      color: var(--ink);
      font-feature-settings: 'tnum' 1, 'zero' 1;
      letter-spacing: 0.005em;
      line-height: 1.45;
      min-height: 100vh;
      overflow-x: hidden;
    }

    /* Subtle scan-line + vignette atmosphere */
    body::before {
      content: '';
      position: fixed; inset: 0; pointer-events: none; z-index: 1;
      background: repeating-linear-gradient(
        0deg, transparent 0, transparent 3px,
        rgba(255,255,255,0.012) 3px, rgba(255,255,255,0.012) 4px);
    }
    body::after {
      content: '';
      position: fixed; inset: 0; pointer-events: none; z-index: 1;
      background:
        radial-gradient(ellipse 80% 60% at 50% 0%, rgba(92, 255, 175, 0.06), transparent 60%),
        radial-gradient(ellipse at center, transparent 0%, rgba(0,0,0,0.5) 100%);
    }

    main {
      position: relative; z-index: 2;
      max-width: 1800px;
      margin: 0 auto;
      padding: 32px 40px 48px;
    }

    /* === HEADER === */
    header {
      display: grid;
      grid-template-columns: minmax(240px, 1.2fr) 1fr auto auto auto;
      gap: 32px;
      align-items: end;
      padding-bottom: 24px;
      margin-bottom: 28px;
      border-bottom: 1px solid var(--line);
    }
    .brand { display: flex; flex-direction: column; gap: 6px; }
    .brand .ident {
      font-size: 9px; letter-spacing: 0.32em;
      color: var(--ink-dim); text-transform: uppercase;
    }
    .brand h1 {
      font-family: 'Major Mono Display', 'JetBrains Mono', monospace;
      font-size: 26px; font-weight: 400;
      letter-spacing: 0.02em; line-height: 1.05;
      color: var(--ink);
    }
    .brand .sub {
      font-size: 10px; color: var(--ink-dim);
      letter-spacing: 0.1em; margin-top: 2px;
    }

    .clock {
      display: flex; flex-direction: column; gap: 4px;
      align-items: flex-end; font-variant-numeric: tabular-nums;
    }
    .clock .time {
      font-size: 22px; font-weight: 100;
      letter-spacing: 0.04em;
    }
    .clock .date {
      font-size: 9px; color: var(--ink-dim);
      letter-spacing: 0.25em; text-transform: uppercase;
    }

    .count {
      display: flex; flex-direction: column; gap: 2px;
      align-items: flex-end;
    }
    .count .num {
      font-size: 56px; font-weight: 100; line-height: 1;
      font-variant-numeric: tabular-nums;
      color: var(--up);
      transition: color 0.4s ease;
    }
    .count.has-down .num { color: var(--down); }
    .count .label {
      font-size: 9px; letter-spacing: 0.3em;
      color: var(--ink-dim); text-transform: uppercase;
      transition: color 0.4s ease;
    }
    .count.has-down .label { color: var(--down); }

    .alarm-control {
      display: flex; flex-direction: column; gap: 4px; align-items: flex-end;
    }
    .alarm-btn {
      background: transparent;
      border: 1px solid var(--line-bright);
      color: var(--ink);
      padding: 10px 18px;
      font-family: inherit;
      font-size: 10px;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      cursor: pointer;
      transition: all 0.2s;
      font-weight: 700;
    }
    .alarm-btn:hover { border-color: var(--up); color: var(--up); }
    .alarm-btn.armed { border-color: var(--up); color: var(--up); }
    .alarm-btn.armed::before { content: '◉ '; }
    .alarm-btn.disarmed::before { content: '○ '; }
    .alarm-status {
      font-size: 8px; letter-spacing: 0.22em;
      color: var(--ink-mute); text-transform: uppercase;
    }

    /* === STATUS BANNER === */
    .banner {
      padding: 14px 0;
      margin-bottom: 32px;
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .banner .pulse {
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--up);
      box-shadow: 0 0 16px var(--up);
      animation: pulse 2s infinite ease-in-out;
    }
    .banner.alert .pulse {
      background: var(--down);
      box-shadow: 0 0 16px var(--down);
      animation: pulse-fast 0.8s infinite ease-in-out;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50%      { opacity: 0.4; transform: scale(0.8); }
    }
    @keyframes pulse-fast {
      0%, 100% { opacity: 1; transform: scale(1.1); }
      50%      { opacity: 0.3; transform: scale(0.7); }
    }
    .banner .msg {
      font-size: 11px; letter-spacing: 0.2em;
      text-transform: uppercase;
      color: var(--ink);
    }
    .banner.alert .msg { color: var(--down); }
    .banner .ticker {
      margin-left: auto; font-size: 9px;
      color: var(--ink-dim); letter-spacing: 0.15em;
      text-transform: uppercase;
    }

    /* === SECTIONS === */
    .grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 28px 32px;
    }
    @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }

    .section { display: flex; flex-direction: column; gap: 12px; }

    .section-head {
      display: flex; align-items: baseline; gap: 10px;
      padding-bottom: 6px;
      border-bottom: 1px dashed var(--line-dash);
    }
    .section-head .num {
      font-size: 10px; color: var(--ink-mute);
      letter-spacing: 0.25em;
      font-variant-numeric: tabular-nums;
    }
    .section-head h2 {
      font-size: 11px; font-weight: 800;
      letter-spacing: 0.28em; text-transform: uppercase;
    }
    .section-head .summary {
      margin-left: auto; font-size: 9px;
      color: var(--ink-dim); letter-spacing: 0.18em;
      text-transform: uppercase;
    }
    .section-head .summary.alert { color: var(--down); }

    /* === CARDS === */
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 8px;
    }

    .card {
      position: relative;
      background: var(--surface-2);
      border: 1px solid var(--line);
      padding: 12px 14px;
      transition: border-color 0.3s ease, background 0.3s ease;
      overflow: hidden;
    }
    .card:hover { border-color: var(--line-bright); }
    .card[data-state="UP"] { border-left: 2px solid var(--up); }
    .card[data-state="DOWN"] {
      border-left: 2px solid var(--down);
      background: linear-gradient(135deg, var(--down-glow), var(--surface-2));
    }
    .card[data-just-failed]::before {
      content: ''; position: absolute; inset: 0; pointer-events: none;
      background: var(--down); opacity: 0.55;
      animation: flash-down 0.7s ease-out 4 forwards;
    }
    .card[data-just-recovered]::after {
      content: ''; position: absolute; inset: 0; pointer-events: none;
      background: var(--up); opacity: 0.4;
      animation: flash-up 0.9s ease-out forwards;
    }
    @keyframes flash-down { 0%,100% { opacity: 0; } 50% { opacity: 0.45; } }
    @keyframes flash-up   { 0% { opacity: 0.4; } 100% { opacity: 0; } }

    .card-head {
      display: flex; align-items: center; gap: 8px;
      margin-bottom: 10px;
    }
    .card-head .dot {
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
      background: var(--up);
      box-shadow: 0 0 8px var(--up);
      animation: pulse 2s infinite ease-in-out;
    }
    .card[data-state="DOWN"] .dot {
      background: var(--down);
      box-shadow: 0 0 10px var(--down);
      animation: pulse-fast 0.8s infinite ease-in-out;
    }
    .card-head .name {
      font-size: 10.5px; font-weight: 700;
      letter-spacing: 0.02em;
      flex: 1; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap;
    }
    .card-head .state {
      font-size: 8.5px; letter-spacing: 0.24em;
      color: var(--up); font-weight: 800;
    }
    .card[data-state="DOWN"] .state { color: var(--down); }

    .card-body {
      display: flex; flex-direction: column; gap: 3px;
      font-size: 9.5px;
    }
    .card-body .row {
      display: flex; justify-content: space-between; gap: 8px;
    }
    .card-body .key {
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--ink-mute);
      font-size: 8.5px;
    }
    .card-body .val {
      font-variant-numeric: tabular-nums;
      color: var(--ink-dim);
      text-align: right;
    }

    .card-err {
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed rgba(255, 77, 87, 0.4);
      font-size: 9.5px;
      color: var(--down);
      word-break: break-word;
      letter-spacing: 0.02em;
    }

    /* === LOADING STATE === */
    .loading {
      grid-column: 1 / -1;
      padding: 80px 0; text-align: center;
      font-size: 10px; letter-spacing: 0.3em;
      color: var(--ink-dim); text-transform: uppercase;
    }
    .loading::after {
      content: ' ▮';
      animation: blink 1s steps(2) infinite;
    }
    @keyframes blink { 50% { opacity: 0; } }

    /* === FOOTER === */
    footer {
      margin-top: 48px; padding-top: 16px;
      border-top: 1px solid var(--line);
      display: flex; justify-content: space-between; align-items: center;
      font-size: 9px; color: var(--ink-mute);
      letter-spacing: 0.25em; text-transform: uppercase;
    }
    footer a {
      color: var(--ink-dim); text-decoration: none;
      letter-spacing: 0.25em;
      padding: 4px 0;
      border-bottom: 1px solid transparent;
      transition: all 0.2s;
    }
    footer a:hover { color: var(--up); border-bottom-color: var(--up); }
    footer .dot-sep { margin: 0 14px; color: var(--ink-mute); }

    @media (max-width: 900px) {
      header { grid-template-columns: 1fr 1fr; gap: 20px; }
      .clock, .count, .alarm-control { align-items: flex-start; }
      main { padding: 20px 18px 32px; }
      .brand h1 { font-size: 20px; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div class="brand">
        <span class="ident">PAS · SCADA · KAFKA · BRIDGE</span>
        <h1>Mission Control</h1>
        <span class="sub">live operational health · 30s probe cycle</span>
      </div>
      <div></div>
      <div class="clock">
        <span class="time" id="clock-time">--:--:--</span>
        <span class="date" id="clock-date">----</span>
      </div>
      <div class="count" id="counter">
        <span class="num" id="count-num">--</span>
        <span class="label" id="count-label">probes</span>
      </div>
      <div class="alarm-control">
        <button class="alarm-btn disarmed" id="alarm-toggle">Enable alarm</button>
        <span class="alarm-status" id="alarm-status">Click to arm sound</span>
      </div>
    </header>

    <div class="banner" id="banner">
      <span class="pulse"></span>
      <span class="msg" id="banner-msg">Initialising…</span>
      <span class="ticker" id="ticker">— sync pending</span>
    </div>

    <div class="grid" id="grid">
      <div class="loading">Loading probe state</div>
    </div>

    <footer>
      <span>PAS-SCADA · Mission Control · 5s refresh</span>
      <div>
        <a href="/state">JSON state</a>
        <span class="dot-sep">/</span>
        <a href="/docs">API</a>
        <span class="dot-sep">/</span>
        <a href="/healthz">healthz</a>
      </div>
    </footer>
  </main>

  <script>
    // ─── Section grouping ────────────────────────────────────────
    const SECTIONS = [
      { id: 'pipeline',   num: '01', title: 'TMS Pipeline',     match: n => /^(artemis-|kafka$|zookeeper$)/.test(n) },
      { id: 'bridge',     num: '02', title: 'Bridge & Workers', match: n => /^(pas-scada-(bridge|demo|monitor)|kafdrop|kafka-connect)$/.test(n) },
      { id: 'connectors', num: '03', title: 'Kafka Connect',    match: n => /^connector-/.test(n) },
      { id: 'scada',      num: '04', title: 'SCADA Side',       match: n => /^(rabbitmq-|scada-api)/.test(n) },
    ];

    // ─── Audio (Web Audio API — synthesized 3-tone alarm) ────────
    let audioCtx = null;
    let alarmInterval = null;
    let alarmArmed = localStorage.getItem('pas-alarm-armed') === '1';

    const ensureAudio = () => audioCtx ||= new (window.AudioContext || window.webkitAudioContext)();

    function beep(freq, duration, when = 0) {
      const ctx = ensureAudio();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      const t0 = ctx.currentTime + when;
      gain.gain.setValueAtTime(0, t0);
      gain.gain.linearRampToValueAtTime(0.22, t0 + 0.02);
      gain.gain.linearRampToValueAtTime(0, t0 + duration);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + duration);
    }
    const playAlarm = () => {
      if (!alarmArmed) return;
      beep(880, 0.18, 0.00);
      beep(740, 0.18, 0.22);
      beep(600, 0.28, 0.44);
    };
    const startAlarm = () => {
      stopAlarm();
      playAlarm();
      alarmInterval = setInterval(playAlarm, 4000);
    };
    const stopAlarm = () => {
      if (alarmInterval) { clearInterval(alarmInterval); alarmInterval = null; }
    };

    // ─── Alarm toggle ────────────────────────────────────────────
    const toggleBtn = document.getElementById('alarm-toggle');
    const toggleStatus = document.getElementById('alarm-status');
    function renderToggle() {
      if (alarmArmed) {
        toggleBtn.textContent = 'Alarm armed';
        toggleBtn.className = 'alarm-btn armed';
        toggleStatus.textContent = 'Audio alerts active';
      } else {
        toggleBtn.textContent = 'Enable alarm';
        toggleBtn.className = 'alarm-btn disarmed';
        toggleStatus.textContent = 'Click to arm sound';
      }
    }
    toggleBtn.addEventListener('click', () => {
      alarmArmed = !alarmArmed;
      localStorage.setItem('pas-alarm-armed', alarmArmed ? '1' : '0');
      renderToggle();
      if (alarmArmed) {
        ensureAudio();
        beep(660, 0.15, 0); // confirmation chirp
      } else {
        stopAlarm();
      }
    });
    renderToggle();

    // ─── Clock ───────────────────────────────────────────────────
    const clockTime = document.getElementById('clock-time');
    const clockDate = document.getElementById('clock-date');
    function updateClock() {
      const now = new Date();
      clockTime.textContent = now.toTimeString().slice(0, 8);
      const dStr = now.toLocaleDateString('en-GB',
        { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
      clockDate.textContent = dStr.toUpperCase().replace(/,/g, ' ·');
    }
    updateClock();
    setInterval(updateClock, 1000);

    // ─── State + render ──────────────────────────────────────────
    const grid = document.getElementById('grid');
    const banner = document.getElementById('banner');
    const bannerMsg = document.getElementById('banner-msg');
    const counter = document.getElementById('counter');
    const countNum = document.getElementById('count-num');
    const countLabel = document.getElementById('count-label');
    let lastStates = {};
    let firstRender = true;

    const escapeHtml = s => String(s).replace(/[&<>"']/g, ch => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[ch]));
    const fmtTime = v => {
      if (!v) return '—';
      const s = String(v);
      const m = s.match(/T(\d{2}:\d{2}:\d{2})/);
      return m ? m[1] : s.slice(0, 8);
    };

    function categorize(state) {
      const buckets = SECTIONS.map(s => ({ ...s, items: [] }));
      const orphans = [];
      Object.keys(state).sort().forEach(name => {
        const item = { name, ...state[name] };
        let placed = false;
        for (const b of buckets) {
          if (b.match(name)) { b.items.push(item); placed = true; break; }
        }
        if (!placed) orphans.push(item);
      });
      if (orphans.length) buckets.push({ id: 'other', num: '99', title: 'Other', items: orphans });
      return buckets;
    }

    function renderCard(c, justFailed, justRecovered) {
      const errBlock = c.current_state === 'DOWN' && c.last_error
        ? `<div class="card-err">${escapeHtml(c.last_error)}</div>`
        : '';
      const failAttr = justFailed ? 'data-just-failed' : '';
      const recAttr  = justRecovered ? 'data-just-recovered' : '';
      return `
        <div class="card" data-state="${c.current_state}" data-name="${escapeHtml(c.name)}" ${failAttr} ${recAttr}>
          <div class="card-head">
            <span class="dot"></span>
            <span class="name">${escapeHtml(c.name)}</span>
            <span class="state">${c.current_state}</span>
          </div>
          <div class="card-body">
            <div class="row"><span class="key">last ok</span><span class="val">${fmtTime(c.last_ok_at)}</span></div>
            <div class="row"><span class="key">last fail</span><span class="val">${fmtTime(c.last_fail_at)}</span></div>
            <div class="row"><span class="key">since</span><span class="val">${fmtTime(c.last_state_change)}</span></div>
            <div class="row"><span class="key">streak</span><span class="val">${c.consecutive_fails || 0}f / ${c.consecutive_passes || 0}p</span></div>
            ${errBlock}
          </div>
        </div>`;
    }

    function render(state) {
      const buckets = categorize(state);
      let html = '';
      for (const b of buckets) {
        const total = b.items.length;
        const downs = b.items.filter(i => i.current_state === 'DOWN').length;
        const ups   = total - downs;
        const summary = downs > 0
          ? `<span class="summary alert">${ups}/${total} OK · ${downs} DOWN</span>`
          : `<span class="summary">${total} green</span>`;
        const cards = b.items.map(item => {
          const prev = lastStates[item.name];
          const justFailed   = !firstRender && prev && prev !== 'DOWN' && item.current_state === 'DOWN';
          const justRecovered = !firstRender && prev && prev !== 'UP'   && item.current_state === 'UP';
          return renderCard(item, justFailed, justRecovered);
        }).join('');
        html += `
          <section class="section">
            <div class="section-head">
              <span class="num">${b.num}</span>
              <h2>${b.title}</h2>
              ${summary}
            </div>
            <div class="cards">${cards}</div>
          </section>`;
      }
      grid.innerHTML = html;
      firstRender = false;
    }

    function updateBanner(state) {
      const items = Object.values(state);
      const total = items.length;
      const downItems = items.filter(i => i.current_state === 'DOWN');
      const ups = total - downItems.length;

      countNum.textContent = downItems.length === 0 ? ups : downItems.length;
      countLabel.textContent = downItems.length === 0 ? 'all green' : 'critical';

      if (downItems.length === 0) {
        banner.classList.remove('alert');
        counter.classList.remove('has-down');
        bannerMsg.textContent = 'All systems nominal';
        document.title = 'PAS-SCADA · all green';
      } else {
        banner.classList.add('alert');
        counter.classList.add('has-down');
        const names = downItems.map(d => d.name).join(' · ');
        bannerMsg.textContent = `${downItems.length} system${downItems.length === 1 ? '' : 's'} down — ${names}`;
        document.title = `🔴 (${downItems.length}) PAS-SCADA — ${downItems.map(d => d.name).join(', ')}`;
      }
    }

    function detectAndAlert(state) {
      let newDownAppeared = false;
      const downSet = new Set();
      for (const name of Object.keys(state)) {
        const cur = state[name].current_state;
        const prev = lastStates[name];
        if (prev && prev !== 'DOWN' && cur === 'DOWN') newDownAppeared = true;
        if (cur === 'DOWN') downSet.add(name);
        lastStates[name] = cur;
      }
      if (downSet.size === 0) {
        stopAlarm();
      } else if (newDownAppeared) {
        startAlarm();
      }
    }

    // ─── Poll loop ───────────────────────────────────────────────
    let lastSync = Date.now();
    const ticker = document.getElementById('ticker');

    async function poll() {
      try {
        const r = await fetch('/state', { cache: 'no-store' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const state = await r.json();
        detectAndAlert(state);
        render(state);
        updateBanner(state);
        lastSync = Date.now();
        ticker.textContent = '— synced just now';
      } catch (e) {
        ticker.textContent = '— sync failed: ' + e.message;
      }
    }
    function refreshTicker() {
      const t = Math.floor((Date.now() - lastSync) / 1000);
      if (t >= 2) ticker.textContent = `— synced ${t}s ago`;
    }
    setInterval(refreshTicker, 1000);
    poll();
    setInterval(poll, 5000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, summary="Visual dashboard")
def dashboard():
    """Mission-control style live dashboard. JS polls /state every 5s,
    categorizes probes into 4 sections, plays an audible alarm on
    state-change UP→DOWN once the user clicks the Enable Alarm button."""
    return HTMLResponse(DASHBOARD_HTML)


# ─── Entrypoint ───────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("PAS-SCADA Monitor starting (FastAPI on :%d)", HEALTH_PORT)
    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=HEALTH_PORT,
            log_level=LOG_LEVEL.lower(),
            access_log=False,  # suppress noise — every k8s probe is a hit
        )
    except KeyboardInterrupt:
        log.info("shutdown requested")
        sys.exit(0)
