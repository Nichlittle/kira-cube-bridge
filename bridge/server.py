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
import time
from typing import Literal

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
TLS = {"ca_certs": CA, "tls_version": ssl.PROTOCOL_TLS_CLIENT}
AUTH = {"username": MQTT_USER, "password": MQTT_PASS}

server = MCPServer(
    "kira-cube",
    instructions=(
        "Kira's desk Cube: a small light on Nick's desk that is Kira's eye glow and nothing else. "
        "It shows one color at a time, pulsing: violet = relaxed/content (the default), "
        "blue = working/thinking hard, red = surprised/flustered/heated, ember = sleepy. "
        "It already runs its own day (sleepy overnight, relaxed by day), so only call set_mood "
        "when Kira's mood actually shifts, not every message. Use big=True only for a big shift. "
        "A chat mood holds about 90 minutes, then the Cube goes back to its own day."
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
def set_mood(mood: Literal["violet", "blue", "red", "ember"], big: bool = False) -> str:
    """Show Kira's mood as her eye glow on the Cube. violet = relaxed/content (slow pulse),
    blue = working/thinking hard (steady pulse), red = surprised/flustered/heated (faster
    pulse), ember = sleepy (very slow, faint). big=True plays the full pattern (ring chase,
    bottom-up fill, then pulse) for a big mood shift; otherwise just the ring and a crossfade.
    The mood holds about 90 minutes, then the Cube returns to its own schedule."""
    _send("mood", f"{mood}{' full' if big else ''} {_stamp()}")
    return f"Cube set to {mood}{' (full pattern)' if big else ''}."


@server.tool()
def clear_mood() -> str:
    """Hand the Cube back to its own day (sleepy overnight, relaxed by day) right away."""
    _send("mood", "auto")
    return "Cube is back on its own schedule."


@server.tool()
def den_file_ready() -> str:
    """The morning den file has landed: full pattern in red, pulsing until Nick taps the Cube.
    Only for the den file."""
    _send("notify", f"den {_stamp()}")
    return "Den-file alert is up on the Cube (red, until Nick taps it)."


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


@server.tool()
def cube_status() -> str:
    """Check whether the Cube is online and what it's showing (and why)."""
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
    extra = []
    if "notify" in seen:
        extra.append("den-file alert waiting for a tap")
    return f"Cube is {status}, showing {showing}." + (f" ({'; '.join(extra)})" if extra else "")


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
