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

Two public, secret-free routes sit beside it:
  GET /healthz      "I'm awake" for the host and a keep-warm pinger
  GET /den/state    the Cube's current mood, read-only, for the den page on
                    nichlittle.github.io (mood, source, online, updated, screen; nothing else)
"""
import os
import re
import ssl
import sys
import json
import time
import asyncio
import threading
from datetime import datetime
from typing import Literal, Union
from zoneinfo import ZoneInfo

import certifi
import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations


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


# ---------------------------------------------------------------------------
# Kira's den: the Cube's mood plus the den page's daily content.
#
# One long-lived MQTT subscriber (started on first use) keeps the Cube's retained
# status/state and the den's retained topics in memory, so reads are dictionary
# lookups instead of fresh broker connections. Everything the den shows lives on
# the broker as retained messages, so it survives Render restarts:
#   den/screen   {"items": [...], "set": t}      the THINKING ABOUT lines
#   den/look     {"look": name, "set": t}        today's rendered look
#   den/brief    {"markdown": str, "set": t}     the morning brief
#   den/closet   {"looks": [{name, note, added}]} looks that have renders
# The private den page (a claude.ai artifact) reads all of it with den_state.
# GET /den/state is the older public feed (mood and screen only); it goes away
# once the GitHub-hosted den is retired.
# ---------------------------------------------------------------------------
DEN_ORIGIN = os.environ.get("DEN_ORIGIN", "https://nichlittle.github.io")
DEN_MOODS = ("rain", "violet", "blue", "red", "ember")
DEN_SOURCES = ("schedule", "chat", "task")
DEN_WARMUP_S = 3  # after a cold start, wait this long at most for the retained values
DEN_SCREEN_TOPIC = "den/screen"
DEN_LOOK_TOPIC = "den/look"
DEN_BRIEF_TOPIC = "den/brief"
DEN_CLOSET_TOPIC = "den/closet"
DEN_SCREEN_MAX_ITEMS = 4
DEN_SCREEN_MAX_CHARS = 40
DEN_LOOK_NAME_MAX = 60
DEN_LOOK_NOTE_MAX = 200
DEN_CLOSET_MAX = 200
DEN_BRIEF_MAX = 30000

_den = {"status": None, "state": None, "updated": None, "started": None, "connected": None,
        "screen": None, "look": None, "brief": None, "closet": None}
_den_lock = threading.Lock()


def _den_on_connect(c, u, flags, rc, props=None):
    c.subscribe([(f"{PREFIX}/status", 0), (f"{PREFIX}/state", 0), (f"{PREFIX}/den/#", 0)])
    _den["connected"] = time.time()


def _json(raw):
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _clean_line(text, limit):
    text = "".join(ch if ch.isprintable() else (" " if ch.isspace() else "") for ch in text)
    return " ".join(text.split())[:limit]


def _clean_screen_item(text):
    return _clean_line(text, DEN_SCREEN_MAX_CHARS)


def _den_parse_screen(raw):
    items = (_json(raw) or {}).get("items")
    if not isinstance(items, list):
        return None
    clean = [_clean_screen_item(i) for i in items if isinstance(i, str)]
    clean = [i for i in clean if i][:DEN_SCREEN_MAX_ITEMS]
    return clean or None


def _den_parse_closet(raw):
    looks = (_json(raw) or {}).get("looks")
    if not isinstance(looks, list):
        return []
    out = []
    for item in looks:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = _clean_line(item["name"], DEN_LOOK_NAME_MAX)
        if name:
            note = item.get("note") if isinstance(item.get("note"), str) else ""
            added = item.get("added") if isinstance(item.get("added"), int) else None
            out.append({"name": name, "note": _clean_line(note, DEN_LOOK_NOTE_MAX), "added": added})
    return out[:DEN_CLOSET_MAX]


def _den_on_message(c, u, m):
    key = m.topic[len(PREFIX) + 1:]
    value = m.payload.decode(errors="replace").strip() if m.payload else None
    if key == DEN_SCREEN_TOPIC:
        _den["screen"] = _den_parse_screen(value) if value else None
    elif key == DEN_LOOK_TOPIC:
        look = (_json(value) or {}).get("look") if value else None
        _den["look"] = _clean_line(look, DEN_LOOK_NAME_MAX) if isinstance(look, str) and look else None
    elif key == DEN_BRIEF_TOPIC:
        b = _json(value) if value else None
        md = b.get("markdown") if b else None
        _den["brief"] = ({"markdown": md[:DEN_BRIEF_MAX], "set": b.get("set")}
                         if isinstance(md, str) and md.strip() else None)
    elif key == DEN_CLOSET_TOPIC:
        _den["closet"] = _den_parse_closet(value) if value else []
    elif key in ("status", "state"):
        if _den.get(key) != value:
            _den[key] = value
            if key == "state":
                _den["updated"] = time.time()


def _den_start():
    with _den_lock:
        if _den["started"] is not None:
            return
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        c.username_pw_set(MQTT_USER, MQTT_PASS)
        c.tls_set(ca_certs=CA)
        c.on_connect, c.on_message = _den_on_connect, _den_on_message
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        c.connect_async(MQTT_HOST, MQTT_PORT)
        c.loop_start()  # paho keeps reconnecting on its own if the broker drops
        _den["started"] = time.time()


def _den_ready_blocking():
    """Start the subscriber and give the broker a moment to hand back retained values.
    Returns True once the subscriber has been connected for at least a second."""
    _den_start()
    deadline = _den["started"] + DEN_WARMUP_S
    while time.time() < deadline:
        if _den["connected"] and time.time() - _den["connected"] >= 1.0:
            return True
        time.sleep(0.1)
    return bool(_den["connected"])


def den_state(status, state, updated, now=None, screen=None):
    """Turn the Cube's retained status/state strings (and the den screen) into the
    public feed's small JSON."""
    s = (state or "").strip().lower()
    paren = re.search(r"\(([^)]*)\)", s)
    src = paren.group(1).strip() if paren else ""
    first = s.split(" ", 1)[0] if s else ""
    if "off" in s and "duty" in s:
        mood, source = "off_duty", "off_duty"
    else:
        mood = first if first in DEN_MOODS else None
        source = src if src in DEN_SOURCES else ("schedule" if mood == "rain" else None)
    stamp = updated if updated is not None else (now if now is not None else time.time())
    return {
        "mood": mood,
        "source": source,
        "online": (status or "").strip().lower() == "online",
        "updated": datetime.fromtimestamp(stamp, CHICAGO).isoformat(timespec="seconds"),
        "screen": list(screen) if screen else None,
    }


def den_snapshot():
    """Everything the private den page shows, as one dict."""
    body = den_state(_den["status"], _den["state"], _den["updated"], screen=_den["screen"])
    body["look"] = _den["look"]
    body["brief"] = _den["brief"]
    body["closet"] = list(_den["closet"] or [])
    return body


@server.custom_route("/den/state", methods=["GET", "HEAD"])
async def den_state_route(request):
    """Public, read-only mood for the old GitHub-hosted den page. Only
    nichlittle.github.io may read it from a browser."""
    from starlette.responses import JSONResponse
    _den_start()
    while _den["state"] is None and time.time() - _den["started"] < DEN_WARMUP_S:
        await asyncio.sleep(0.1)
    body = den_state(_den["status"], _den["state"], _den["updated"], screen=_den["screen"])
    return JSONResponse(body, headers={
        "Access-Control-Allow-Origin": DEN_ORIGIN,
        "Vary": "Origin",
        "Cache-Control": "no-store",
    })


@server.tool(name="den_state", annotations=ToolAnnotations(readOnlyHint=True))
def den_state_tool() -> dict:
    """Read everything Kira's private den page shows: the Cube's mood (mood, source,
    online, updated), the THINKING ABOUT lines (screen), today's look (look), the morning
    brief (brief: {markdown, set}) and the closet of rendered looks (closet: [{name, note,
    added}]). Read-only."""
    _den_ready_blocking()
    return den_snapshot()


@server.tool()
def set_den_screen(items: list[str]) -> str:
    """Put the day's THINKING ABOUT lines on Kira's den page. Pass 1-4 short lines
    (3-6 words each works best), exactly as they should read on the screen. They replace
    whatever was there and stay until the next call."""
    clean = [_clean_screen_item(i) for i in items if isinstance(i, str)]
    clean = [i for i in clean if i]
    if not clean:
        raise ToolError("Give at least one non-empty line.")
    if len(clean) > DEN_SCREEN_MAX_ITEMS:
        raise ToolError(f"The screen holds at most {DEN_SCREEN_MAX_ITEMS} lines.")
    _send(DEN_SCREEN_TOPIC, json.dumps({"items": clean, "set": int(time.time())}))
    _den["screen"] = clean  # the cached subscriber would catch it too; this skips the wait
    trimmed = any(len(" ".join(i.split())) > DEN_SCREEN_MAX_CHARS for i in items if isinstance(i, str))
    note = f" (lines are capped at {DEN_SCREEN_MAX_CHARS} characters; one was trimmed)" if trimmed else ""
    return f"Den screen set: {' / '.join(clean)}{note}."


def _closet_now():
    if not _den_ready_blocking():
        raise ToolError("Couldn't reach the Cube's MQTT broker to read the closet; nothing changed.")
    return list(_den["closet"] or [])


def _find_look(closet, name):
    want = _clean_line(name or "", DEN_LOOK_NAME_MAX).casefold()
    for item in closet:
        if item["name"].casefold() == want:
            return item
    return None


@server.tool()
def set_den_look(name: str) -> str:
    """Choose today's look on Kira's den page. `name` must match a look in the closet
    (see den_state's closet list; case doesn't matter)."""
    closet = _closet_now()
    item = _find_look(closet, name)
    if not item:
        names = ", ".join(i["name"] for i in closet) or "(the closet is empty)"
        raise ToolError(f"No look named {name!r} in the closet. Looks: {names}")
    _send(DEN_LOOK_TOPIC, json.dumps({"look": item["name"], "set": int(time.time())}))
    _den["look"] = item["name"]
    return f"Today's look: {item['name']}."


@server.tool()
def add_den_look(name: str, note: str = "") -> str:
    """Add a rendered look to the den's closet (or update its note if it's already there).
    The den page calls this when Nick uploads a render; `note` is a short description the
    morning task can use when picking (outfit, pose, vibe)."""
    clean = _clean_line(name or "", DEN_LOOK_NAME_MAX)
    if not clean:
        raise ToolError("Give the look a name.")
    note = _clean_line(note or "", DEN_LOOK_NOTE_MAX)
    closet = _closet_now()
    item = _find_look(closet, clean)
    if item:
        item["note"] = note or item["note"]
    else:
        if len(closet) >= DEN_CLOSET_MAX:
            raise ToolError(f"The closet holds at most {DEN_CLOSET_MAX} looks.")
        closet.append({"name": clean, "note": note, "added": int(time.time())})
    _send(DEN_CLOSET_TOPIC, json.dumps({"looks": closet}))
    _den["closet"] = closet
    return f"{'Updated' if item else 'Added'} {clean!r}; the closet has {len(closet)} look{'s' if len(closet) != 1 else ''}."


@server.tool()
def remove_den_look(name: str) -> str:
    """Take a look out of the den's closet (the render itself stays in the den page's
    storage). If it was today's look, the page falls back to the empty room."""
    closet = _closet_now()
    item = _find_look(closet, name)
    if not item:
        raise ToolError(f"No look named {name!r} in the closet.")
    closet = [i for i in closet if i is not item]
    _send(DEN_CLOSET_TOPIC, json.dumps({"looks": closet}))
    _den["closet"] = closet
    if _den["look"] and _den["look"].casefold() == item["name"].casefold():
        _send(DEN_LOOK_TOPIC, "")
        _den["look"] = None
    return f"Removed {item['name']!r}; the closet has {len(closet)} look{'s' if len(closet) != 1 else ''}."


@server.tool()
def set_den_brief(markdown: str) -> str:
    """Post the morning brief (weather, outfit picks, news) to Kira's den page, below the
    room. Markdown: headings, bold, bullet lists and links render. Replaces the previous
    brief."""
    md = (markdown or "").strip()
    if not md:
        raise ToolError("The brief is empty.")
    if len(md) > DEN_BRIEF_MAX:
        raise ToolError(f"The brief is too long ({len(md)} characters; max {DEN_BRIEF_MAX}).")
    stamp = int(time.time())
    _send(DEN_BRIEF_TOPIC, json.dumps({"markdown": md, "set": stamp}))
    _den["brief"] = {"markdown": md, "set": stamp}
    return f"Brief posted to the den ({len(md)} characters)."


if __name__ == "__main__":
    server.run(
        "streamable-http",
        host="0.0.0.0",
        port=PORT,
        streamable_http_path=f"/{SECRET}/mcp",
        stateless_http=True,
        json_response=True,
    )
