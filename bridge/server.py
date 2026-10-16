"""Kira Cube bridge — a remote MCP server that lets Kira drive the cube (Phase 4).

Claude (the custom connector) --HTTPS--> this server --MQTT/TLS--> EMQX Cloud --> cube

Settings come from environment variables:
  MQTT_HOST, MQTT_USER, MQTT_PASS   EMQX Cloud deployment + a username for the bridge
  MQTT_PORT                         defaults to 8883
  MQTT_CA_FILE                      optional: broker CA file (default: standard CA bundle)
  TOPIC_PREFIX                      same value as in the cube's config.py
  CUBE_SECRET                       long random string; it becomes part of the URL
  PORT                              defaults to 8000

The connector URL you paste into Claude is:
  https://<your-host>/<CUBE_SECRET>/mcp
Anyone without the secret gets a 404. Treat that URL like a password.
"""
import os
import ssl
import sys
import json
import time
from datetime import datetime
from typing import Literal, Union
from zoneinfo import ZoneInfo

import certifi
import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"Missing environment variable {name}")
    return v


MQTT_HOST = env("MQTT_HOST")
MQTT_USER = env("MQTT_USER")
MQTT_PASS = env("MQTT_PASS")
PREFIX = env("TOPIC_PREFIX").rstrip("/")
SECRET = env("CUBE_SECRET")
PORT = int(env("PORT", "8000"))  # hosts like Render set PORT themselves
if len(SECRET) < 24:
    sys.exit("CUBE_SECRET should be at least 24 characters — it's the only lock on the door.")

MQTT_PORT = int(env("MQTT_PORT", "8883"))
CA = os.environ.get("MQTT_CA_FILE") or certifi.where()
CHICAGO = ZoneInfo("America/Chicago")
TLS = {"ca_certs": CA, "tls_version": ssl.PROTOCOL_TLS_CLIENT}
AUTH = {"username": MQTT_USER, "password": MQTT_PASS}

server = MCPServer(
    "kira-cube",
    log_level="WARNING",   # no per-request access logs: they would print the secret URL path
    instructions=(
        "Kira's desk Cube: a small light on Nick's desk that is Kira's eye glow and nothing else. "
        "By day its idle state is a calm digital rain (blue, violet, pink); overnight it's a sleepy ember. "
        "set_mood shows one of Kira's eye colors over that: violet = relaxed/content, "
        "blue = working/thinking hard, red = surprised/flustered/heated, ember = sleepy. "
        "Red and blue are quick reactions (about 90 seconds); violet and ember hold about 90 minutes. "
        "Pass hold (minutes, or \"long\") for a sustained state. Only call set_mood when Kira's mood "
        "actually shifts, not every message. Use big=True only for a big shift.\n"
    ),
)


def _send(sub, payload, retain=True):
    try:
        publish.single(f"{PREFIX}/{sub}", payload, retain=retain, hostname=MQTT_HOST,
                       port=MQTT_PORT, auth=AUTH, tls=TLS, client_id="", protocol=mqtt.MQTTv311)
    except Exception as e:  # tell Kira plainly instead of a bare crash
        raise ToolError(f"Couldn't reach the Cube's MQTT broker: {e}") from e


def _stamp():
    return f"@{int(time.time())}"


@server.tool()
def set_mood(mood: Literal["violet", "blue", "red", "ember"], big: bool = False,
             hold: Union[Literal["long"], float, None] = None) -> str:
    """Show Kira's mood as her eye glow on the Cube, over its idle digital rain.
    violet = relaxed/content (slow pulse), blue = working/thinking hard (steady pulse),
    red = surprised/flustered/heated (faster pulse), ember = sleepy (very slow, faint).
    big=True plays the full pattern (ring chase, bottom-up fill, then pulse) for a big shift;
    otherwise just the ring and a crossfade.

    How long it shows before the Cube falls back to the rain:
    - red and blue are quick reactions: about 90 seconds by default.
    - violet and ember are held states: about 90 minutes by default.
    - hold overrides that for this one call: a number of minutes (e.g. 30 for a long
      research session), or "long" for 90 minutes.
    Setting the same mood again during a hold restarts its timer and replays the ring."""
    if hold is None:
        seconds = None
    elif hold == "long":
        seconds = 90 * 60
    else:
        try:
            seconds = int(float(hold) * 60)
        except (TypeError, ValueError):
            raise ToolError("hold must be a number of minutes or \"long\".")
        if not 1 <= seconds <= 12 * 3600:
            raise ToolError("hold must be between 1 second and 12 hours (720 minutes).")
    parts = [mood] + (["full"] if big else []) + ([f"hold={seconds}"] if seconds else [])
    _send("mood", f"{' '.join(parts)} {_stamp()}")
    if seconds:
        length = f"for {seconds // 60} min" if seconds >= 60 else f"for {seconds} s"
    else:
        length = "for about 90 s" if mood in ("red", "blue") else "for about 90 min"
    return f"Cube set to {mood}{' (full pattern)' if big else ''} {length}."


@server.tool()
def clear_mood() -> str:
    """Hand the Cube back to its own day (sleepy overnight, relaxed by day) right away."""
    _send("mood", "auto")
    return "Cube is back on its own schedule."


@server.tool()
def task_running(running: bool) -> str:
    """Call with running=True when a scheduled task starts (the Cube goes blue) and
    running=False when it finishes. If the 'finished' call never comes, the Cube gives up
    after about 45 minutes."""
    _send("task", f"start {_stamp()}" if running else "")
    return "Cube shows a task running." if running else "Cube: task finished."


@server.tool()
def set_brightness(percent: int) -> str:
    """Brightness as a percent of the Cube's safe maximum (0-100). The hardware cap can't be
    exceeded from here."""
    p = max(0, min(100, percent))
    _send("bright", str(p))
    return f"Cube brightness {p}%."


def describe_taps(raw, now=None):
    """Turn the Cube's retained tap log into one quiet line for Kira, or ''."""
    try:
        log = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    stamps = log.get("list") or []
    if not stamps:
        return ""
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now, CHICAGO).strftime("%Y-%m-%d")
    count = log.get("count", 0) if log.get("day") == today else 0
    parts = [f"last tap {_ago(now - stamps[-1])}", f"{count} tap{'s' if count != 1 else ''} today"]
    return ", ".join(parts)


def _ago(seconds):
    s = max(0, int(seconds))
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{round(s / 60)} minutes ago"
    if s < 86400:
        h = round(s / 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = round(s / 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


@server.tool()
def cube_status() -> str:
    """Check whether the Cube is online, what it's showing (and why), and when Nick last
    tapped it. Taps are a quiet 'he was here' signal: notice them, don't remark on every one."""
    seen = {}

    def on_connect(c, u, flags, rc, props=None):
        c.subscribe(PREFIX + "/#")

    def on_message(c, u, m):
        if m.retain and m.payload:
            seen[m.topic[len(PREFIX) + 1:]] = m.payload.decode(errors="replace")

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.tls_set(ca_certs=CA)
    c.on_connect, c.on_message = on_connect, on_message
    try:
        c.connect(MQTT_HOST, MQTT_PORT)
    except Exception as e:
        raise ToolError(f"Couldn't reach the Cube's MQTT broker: {e}") from e
    c.loop_start()
    time.sleep(2)
    c.loop_stop()
    c.disconnect()
    status = seen.get("status", "unknown")
    showing = seen.get("state", "unknown")
    out = f"Cube is {status}, showing {showing}."
    taps = describe_taps(seen.get("taps"))
    if taps:
        out += f" Taps: {taps}."
    info = seen.get("info", "")
    if info.startswith("imu="):
        out += f" Motion sensor: {info[4:]}."
    return out


@server.custom_route("/healthz", methods=["GET", "HEAD"])
async def healthz(request):
    """Public, secret-free 'I'm awake' check for the host and a keep-warm pinger."""
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


if __name__ == "__main__":
    server.run(
        "streamable-http",
        host="0.0.0.0",
        port=PORT,
        streamable_http_path=f"/{SECRET}/mcp",
        stateless_http=True,
        json_response=True,
    )
