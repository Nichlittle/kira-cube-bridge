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

One public, secret-free route sits beside it:
  GET /healthz      "I'm awake" for the host and a keep-warm pinger
"""
import os
import re
import ssl
import sys
import json
import time
import base64
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Literal, Union
from zoneinfo import ZoneInfo

import certifi
import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish
from mcp.server.mcpserver import Image, MCPServer
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
        "set_mood shows one of Kira's eye colors over that idle: violet = a warm, content moment "
        "(not the default and not a reset), "
        "blue = working/thinking hard, red = surprised/flustered/heated, ember = sleepy. "
        "Red and blue are quick reactions (about 90 seconds); violet and ember hold about 90 minutes. "
        "Pass hold (minutes, or \"long\") for a sustained state. Only call set_mood when Kira's mood "
        "actually shifts, not every message. Use big=True only for a big shift. To go back to "
        "baseline when a moment has passed, call clear_mood; don't set violet for that.\n"
        "Renders cost money: render_quote first (free), show Nick the price, and call "
        "render_image only after he says yes to that quote. Never retry render_image on your own.\n"
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
    Every mood covers the rain (or overnight ember) until it expires; none is "back to normal".
    violet = a warm, content moment (slow pulse; not the default), blue = working/thinking hard (steady pulse),
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
    _den_set_eyes(mood, seconds or (90 if mood in ("red", "blue") else 90 * 60))
    if seconds:
        length = f"for {seconds // 60} min" if seconds >= 60 else f"for {seconds} s"
    else:
        length = "for about 90 s" if mood in ("red", "blue") else "for about 90 min"
    return f"Cube set to {mood}{' (full pattern)' if big else ''} {length}."


@server.tool()
def clear_mood() -> str:
    """Hand the Cube back to its own idle right away: digital rain by day, ember overnight.
    This is the "back to baseline" call when a moment has passed. Use it instead of
    setting violet."""
    _send("mood", "auto")
    _send(DEN_EYES_TOPIC, "")
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
#   den/weather  {"sky": ..., "temp_f": n, "set": t} today's Chicago sky for the room
#   den/wishlist {"items": [{name, pitch, prompt, status, added}]} Kira's outfit pitches
#   den/eyes     {"mood": m, "set": t, "until": t}  Kira's own eye glow, written by set_mood
# The room's mood comes from den/eyes (what Kira chose), never from the Cube's reported
# state; with no live eye color it follows the clock (rain by day, ember 8:30 p.m.-6:30 a.m.).
# The private den page (a claude.ai artifact) reads all of it with the den_state
# tool; nothing about the den is served publicly.
# ---------------------------------------------------------------------------
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
DEN_WEATHER_TOPIC = "den/weather"
DEN_WISHLIST_TOPIC = "den/wishlist"
DEN_EYES_TOPIC = "den/eyes"
DEN_EYE_MOODS = ("violet", "blue", "red", "ember")
DEN_NIGHT_START = 20 * 60 + 30
DEN_NIGHT_END = 6 * 60 + 30
DEN_SKIES = ("clear", "cloudy", "drizzle", "rain", "storm", "snow")
DEN_WISH_STATUSES = ("open", "cut", "rendered")
DEN_WISH_PITCH_MAX = 300
DEN_WISH_PROMPT_MAX = 4000
DEN_WISH_OPEN_MAX = 12   # pitches waiting on Nick at once
DEN_WISHLIST_MAX = 150   # including cut and rendered ones, kept so nothing is pitched twice

_den = {"status": None, "state": None, "updated": None, "started": None, "connected": None,
        "screen": None, "look": None, "brief": None, "closet": None,
        "weather": None, "wishlist": None, "eyes": None, "renders": None}
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


def _den_parse_weather(raw):
    w = _json(raw)
    if not w or w.get("sky") not in DEN_SKIES:
        return None
    t = w.get("temp_f")
    return {"sky": w["sky"], "temp_f": t if isinstance(t, int) else None,
            "set": w.get("set") if isinstance(w.get("set"), int) else None}


def _den_parse_wishlist(raw):
    items = (_json(raw) or {}).get("items")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str):
            continue
        name = _clean_line(it["name"], DEN_LOOK_NAME_MAX)
        if not name:
            continue
        pitch = it.get("pitch") if isinstance(it.get("pitch"), str) else ""
        prompt = it.get("prompt") if isinstance(it.get("prompt"), str) else ""
        status = it.get("status") if it.get("status") in DEN_WISH_STATUSES else "open"
        added = it.get("added") if isinstance(it.get("added"), int) else None
        out.append({"name": name, "pitch": _clean_line(pitch, DEN_WISH_PITCH_MAX),
                    "prompt": prompt.strip()[:DEN_WISH_PROMPT_MAX], "status": status, "added": added})
    return out[:DEN_WISHLIST_MAX]


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
    elif key == DEN_WEATHER_TOPIC:
        _den["weather"] = _den_parse_weather(value) if value else None
    elif key == DEN_WISHLIST_TOPIC:
        _den["wishlist"] = _den_parse_wishlist(value) if value else []
    elif key == DEN_EYES_TOPIC:
        e = _json(value) if value else None
        _den["eyes"] = (e if e and e.get("mood") in DEN_EYE_MOODS
                        and isinstance(e.get("until"), (int, float)) else None)
    elif key == RENDER_TOPIC:
        _den["renders"] = _render_parse(value) if value else None
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
    mood half of the den's JSON."""
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


def _den_set_eyes(mood, seconds):
    now = int(time.time())
    eyes = {"mood": mood, "set": now, "until": now + int(seconds)}
    _send(DEN_EYES_TOPIC, json.dumps(eyes))
    _den["eyes"] = eyes


def den_eyes_mood(eyes, now_dt):
    """The room's mood from Kira's last eye color, or the clock when none is live."""
    if eyes and eyes.get("until", 0) > now_dt.timestamp():
        return eyes["mood"], "chat", eyes.get("set")
    t = now_dt.hour * 60 + now_dt.minute
    night = t >= DEN_NIGHT_START or t < DEN_NIGHT_END
    return ("ember" if night else "rain"), "schedule", None


def den_snapshot():
    """Everything the private den page shows, as one dict."""
    body = den_state(_den["status"], _den["state"], _den["updated"], screen=_den["screen"])
    mood, source, stamp = den_eyes_mood(_den["eyes"], datetime.now(CHICAGO))
    body["mood"], body["source"] = mood, source
    if stamp:
        body["updated"] = datetime.fromtimestamp(stamp, CHICAGO).isoformat(timespec="seconds")
    body["look"] = _den["look"]
    body["brief"] = _den["brief"]
    body["closet"] = list(_den["closet"] or [])
    body["weather"] = _den["weather"]
    wl = list(_den["wishlist"] or [])
    body["wishlist"] = [i for i in wl if i["status"] == "open"]
    body["wishlist_cut"] = [i["name"] for i in wl if i["status"] == "cut"]
    body["wishlist_rendered"] = [i["name"] for i in wl if i["status"] == "rendered"]
    return body


DEN_URL = "https://claude.ai/artifact/6MKW6A3BmWvEWCi1apXykW"


def _den_moved():
    """Nov 8: the den keeps its own data in the den artifact's database, so the morning task
    and Kira chats no longer depend on this server. The old write tools stay registered only
    to point callers there."""
    raise ToolError(
        "The den moved on Nov 8: it keeps its own data now, and nothing was changed here. "
        "Write to the den artifact's database with the ArtifactData tool (load it with ToolSearch "
        f"\"select:ArtifactData\" if it's deferred), url {DEN_URL}. Today's look, screen, brief and "
        "weather are fields of collection \"den\", doc_id \"today\" (read it first and pass its version "
        "as if_version; use action \"update\" to change only some fields). The closet is collection "
        "\"looks\" (set retired true/false to take a look out of or back into rotation; don't create "
        "looks, they need an image). Pitches are collection \"wishlist\" (doc_id = slug of the name; "
        "status open, cut or rendered).")


@server.tool(name="den_state", annotations=ToolAnnotations(readOnlyHint=True))
def den_state_tool() -> dict:
    """MOOD ONLY since Nov 8: mood, source and online are current; everything else here is a
    frozen copy from before the den moved to its artifact (read collection "den", doc "today",
    "looks" and "wishlist" there with ArtifactData instead). Originally: everything Kira's private den page shows: the room's mood (mood, source,
    updated: Kira's own last set_mood color while it lasts, otherwise the clock; online
    only says whether the Cube is plugged in), the THINKING ABOUT lines (screen), today's look (look), the morning
    brief (brief: {markdown, set}), the closet of rendered looks (closet: [{name, note,
    added}]), today's weather for the room (weather: {sky, temp_f, set}), Kira's open
    outfit pitches (wishlist: [{name, pitch, prompt, status, added}]) and the names of
    pitches Nick cut (wishlist_cut: never pitch these again) or already rendered
    (wishlist_rendered). Read-only."""
    _den_ready_blocking()
    return den_snapshot()


@server.tool(name="den_summary", annotations=ToolAnnotations(readOnlyHint=True))
def den_summary_tool() -> dict:
    """FROZEN since Nov 8 except mood/source/online: the den's content lives in its artifact's
    database now (ArtifactData). Originally: light read of the den: today's look, the THINKING ABOUT lines (screen), the weather,
    the room's mood, when the brief was last set (brief_set, a Unix timestamp), and counts
    for the closet and open Wishlist pitches. It leaves out the brief text, the closet
    notes and the Wishlist prompts, so it is much cheaper than den_state. Use it for
    routine checks and on-demand updates; use den_state only when you need the closet
    notes (picking a look) or the Wishlist. Read-only."""
    _den_ready_blocking()
    snap = den_snapshot()
    brief = snap.get("brief") or {}
    return {
        "mood": snap["mood"],
        "source": snap["source"],
        "online": snap["online"],
        "look": snap["look"],
        "screen": snap["screen"],
        "weather": snap["weather"],
        "brief_set": brief.get("set"),
        "closet_count": len(snap["closet"]),
        "open_pitches": len(snap["wishlist"]),
    }


@server.tool()
def set_den_screen(items: list[str]) -> str:
    """Put the day's THINKING ABOUT lines on Kira's den page. Pass 1-4 short lines
    (3-6 words each works best), exactly as they should read on the screen. They replace
    whatever was there and stay until the next call."""
    _den_moved()
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
    _den_moved()
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
    _den_moved()
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
    _den_moved()
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
    _render_forget_look(item["name"])
    return f"Removed {item['name']!r}; the closet has {len(closet)} look{'s' if len(closet) != 1 else ''}."


@server.tool()
def set_den_brief(markdown: str) -> str:
    """Post the morning brief (weather, outfit picks, news) to Kira's den page, below the
    room. Markdown: headings, bold, bullet lists and links render. Replaces the previous
    brief."""
    _den_moved()
    md = (markdown or "").strip()
    if not md:
        raise ToolError("The brief is empty.")
    if len(md) > DEN_BRIEF_MAX:
        raise ToolError(f"The brief is too long ({len(md)} characters; max {DEN_BRIEF_MAX}).")
    stamp = int(time.time())
    _send(DEN_BRIEF_TOPIC, json.dumps({"markdown": md, "set": stamp}))
    _den["brief"] = {"markdown": md, "set": stamp}
    return f"Brief posted to the den ({len(md)} characters)."


@server.tool()
def set_den_weather(sky: str, temp_f: int | None = None) -> str:
    """Set today's Chicago sky for Kira's den: the city screen rains, drizzles, storms,
    snows or clears to match, and a cold temp_f frosts the glass. sky is one of clear,
    cloudy, drizzle, rain, storm, snow. The morning brief task sets it from the forecast."""
    _den_moved()
    sky = (sky or "").strip().lower()
    if sky not in DEN_SKIES:
        raise ToolError(f"sky must be one of: {', '.join(DEN_SKIES)}.")
    if temp_f is not None and not -60 <= int(temp_f) <= 130:
        raise ToolError("temp_f looks wrong; give degrees Fahrenheit.")
    w = {"sky": sky, "temp_f": int(temp_f) if temp_f is not None else None, "set": int(time.time())}
    _send(DEN_WEATHER_TOPIC, json.dumps(w))
    _den["weather"] = w
    t = f", {w['temp_f']}°F" if w["temp_f"] is not None else ""
    return f"Den weather: {sky}{t}."


@server.tool()
def den_update(look: str | None = None, screen: list[str] | None = None,
               sky: str | None = None, temp_f: int | None = None,
               brief: str | None = None) -> str:
    """Update several parts of the den in one call. Every argument is optional, but pass at
    least one: look (a closet look name), screen (1-4 THINKING ABOUT lines), sky (with an
    optional temp_f), brief (the morning brief markdown). Everything is checked before
    anything is written, so a bad argument changes nothing. Parts you leave out stay as
    they are. The single-purpose tools (set_den_look, set_den_screen, set_den_weather,
    set_den_brief) still work the same way."""
    _den_moved()
    if look is None and screen is None and sky is None and brief is None:
        raise ToolError("Pass at least one of look, screen, sky or brief.")
    if temp_f is not None and sky is None:
        raise ToolError("temp_f goes with sky; pass sky too.")

    item = None
    if look is not None:
        closet = _closet_now()
        item = _find_look(closet, look)
        if not item:
            names = ", ".join(i["name"] for i in closet) or "(the closet is empty)"
            raise ToolError(f"No look named {look!r} in the closet. Looks: {names}")

    clean_screen = None
    if screen is not None:
        clean_screen = [c for c in (_clean_screen_item(i) for i in screen if isinstance(i, str)) if c]
        if not clean_screen:
            raise ToolError("Give at least one non-empty screen line.")
        if len(clean_screen) > DEN_SCREEN_MAX_ITEMS:
            raise ToolError(f"The screen holds at most {DEN_SCREEN_MAX_ITEMS} lines.")

    sky_clean = None
    if sky is not None:
        sky_clean = sky.strip().lower()
        if sky_clean not in DEN_SKIES:
            raise ToolError(f"sky must be one of: {', '.join(DEN_SKIES)}.")
        if temp_f is not None and not -60 <= int(temp_f) <= 130:
            raise ToolError("temp_f looks wrong; give degrees Fahrenheit.")

    md = None
    if brief is not None:
        md = brief.strip()
        if not md:
            raise ToolError("The brief is empty.")
        if len(md) > DEN_BRIEF_MAX:
            raise ToolError(f"The brief is too long ({len(md)} characters; max {DEN_BRIEF_MAX}).")

    stamp = int(time.time())
    done = []
    if item:
        _send(DEN_LOOK_TOPIC, json.dumps({"look": item["name"], "set": stamp}))
        _den["look"] = item["name"]
        done.append(f"look: {item['name']}")
    if clean_screen:
        _send(DEN_SCREEN_TOPIC, json.dumps({"items": clean_screen, "set": stamp}))
        _den["screen"] = clean_screen
        done.append(f"screen: {' / '.join(clean_screen)}")
    if sky_clean:
        w = {"sky": sky_clean, "temp_f": int(temp_f) if temp_f is not None else None, "set": stamp}
        _send(DEN_WEATHER_TOPIC, json.dumps(w))
        _den["weather"] = w
        done.append(f"weather: {sky_clean}" + (f", {w['temp_f']}°F" if w["temp_f"] is not None else ""))
    if md:
        _send(DEN_BRIEF_TOPIC, json.dumps({"markdown": md, "set": stamp}))
        _den["brief"] = {"markdown": md, "set": stamp}
        done.append(f"brief ({len(md)} characters)")
    return "Den updated. " + "; ".join(done) + "."


def _wishlist_now():
    if not _den_ready_blocking():
        raise ToolError("Couldn't reach the Cube's MQTT broker to read the wishlist; nothing changed.")
    return [dict(i) for i in (_den["wishlist"] or [])]


def _save_wishlist(wl):
    _send(DEN_WISHLIST_TOPIC, json.dumps({"items": wl}))
    _den["wishlist"] = wl


@server.tool()
def add_den_suggestion(name: str, pitch: str, prompt: str) -> str:
    """Pitch a new outfit for Kira's wardrobe on the den page's Wishlist. name: a short
    look name. pitch: one or two lines in Kira's voice on what it is and why. prompt: the
    full ready-to-paste Grok image prompt (base block, outfit, den setting, pose,
    expression, style lock, negatives). Nick copies the prompt to render it, or cuts it.
    Names he has cut or already rendered are refused, so nothing is pitched twice."""
    _den_moved()
    clean = _clean_line(name or "", DEN_LOOK_NAME_MAX)
    if not clean:
        raise ToolError("Give the pitch a name.")
    pitch = _clean_line(pitch or "", DEN_WISH_PITCH_MAX)
    prompt = (prompt or "").strip()
    if not prompt:
        raise ToolError("Include the Grok prompt.")
    if len(prompt) > DEN_WISH_PROMPT_MAX:
        raise ToolError(f"The prompt is too long ({len(prompt)} characters; max {DEN_WISH_PROMPT_MAX}).")
    wl = _wishlist_now()
    for i in wl:
        if i["name"].casefold() == clean.casefold():
            if i["status"] != "open":
                raise ToolError(f"{i['name']!r} was already {i['status']}; pitch something new.")
            i.update(pitch=pitch, prompt=prompt)
            _save_wishlist(wl)
            return f"Updated the pitch {i['name']!r}."
    if sum(1 for i in wl if i["status"] == "open") >= DEN_WISH_OPEN_MAX:
        raise ToolError(f"The wishlist already has {DEN_WISH_OPEN_MAX} open pitches; wait for Nick to work through some.")
    wl.append({"name": clean, "pitch": pitch, "prompt": prompt, "status": "open", "added": int(time.time())})
    if len(wl) > DEN_WISHLIST_MAX:  # drop the oldest rendered ones first; cut ones are the memory
        for status in ("rendered", "cut"):
            while len(wl) > DEN_WISHLIST_MAX and any(i["status"] == status for i in wl):
                wl.remove(next(i for i in wl if i["status"] == status))
    _save_wishlist(wl)
    n = sum(1 for i in wl if i["status"] == "open")
    return f"Pitched {clean!r}; {n} open pitch{'es' if n != 1 else ''} on the wishlist."


@server.tool()
def resolve_den_suggestion(name: str, status: str) -> str:
    """Close a Wishlist pitch. status 'cut' = Nick passed ("not my vibe"; never pitch it
    again), 'rendered' = he made it and it's in the closet now, 'open' = put it back."""
    _den_moved()
    status = (status or "").strip().lower()
    if status not in DEN_WISH_STATUSES:
        raise ToolError(f"status must be one of: {', '.join(DEN_WISH_STATUSES)}.")
    wl = _wishlist_now()
    want = _clean_line(name or "", DEN_LOOK_NAME_MAX).casefold()
    for i in wl:
        if i["name"].casefold() == want:
            i["status"] = status
            _save_wishlist(wl)
            return f"{i['name']!r} marked {status}."
    raise ToolError(f"No pitch named {name!r} on the wishlist.")


# ---------------------------------------------------------------------------
# Renders (Nov 5): Grok Imagine through xAI's API, driven from the Kira chat.
#
#   render_quote  (free)      price a render; returns a quote id good for 10 minutes
#   render_image  (spends)    run a quote Nick said yes to; returns the image + a link
#   render_review             approve (with a closet name) or reject; reject deletes it
#   render_inbox / render_fetch / render_filed
#                             the den page collects approved renders, saves them to its
#                             own storage and the closet, then marks them filed
#
# Spend and the render list live in the retained den/renders message, so the daily cap
# survives restarts. Nothing is written to this server's disk. Images go to xAI as base64
# data URIs (the request shape tested by hand on Nov 5); renders and Nick's references
# are kept in xAI's Files API by file id. The key is only ever sent to api.x.ai.
#
# Env: XAI_API_KEY (needs image + files permissions), XAI_REF_FILES (comma-separated
# file ids of the reference images), optional RENDER_DAILY_CAP_USD (default 0.50),
# XAI_IMAGE_MODEL (default grok-imagine-image-2.0), XAI_IMAGE_RESOLUTION (1k or 2k; unset
# sends nothing, so xAI uses 1k) and XAI_IMAGE_QUALITY (low, medium or auto; unset sends
# nothing, and xAI's auto is medium for edits). Without the key or references the render
# tools say so; every other tool works as before.
# ---------------------------------------------------------------------------
XAI_KEY = os.environ.get("XAI_API_KEY", "").strip()
XAI_BASE = "https://api.x.ai/v1"
XAI_MODEL = os.environ.get("XAI_IMAGE_MODEL", "grok-imagine-image-2.0").strip()
# Optional output settings, sent only when set. Values outside xAI's documented ones are
# ignored rather than sent. xAI's docs don't say whether the edits endpoint honors
# resolution, so check the first 2k render's pixel size.
XAI_RESOLUTION = os.environ.get("XAI_IMAGE_RESOLUTION", "").strip().lower()
if XAI_RESOLUTION not in ("", "1k", "2k"):
    XAI_RESOLUTION = ""
XAI_QUALITY = os.environ.get("XAI_IMAGE_QUALITY", "").strip().lower()
if XAI_QUALITY not in ("", "low", "medium", "auto"):
    XAI_QUALITY = ""
XAI_REF_FILES = [f.strip() for f in os.environ.get("XAI_REF_FILES", "").split(",") if f.strip()]
try:
    RENDER_DAILY_CAP = float(os.environ.get("RENDER_DAILY_CAP_USD", "0.50"))
except ValueError:
    RENDER_DAILY_CAP = 0.50
RENDER_TOPIC = "den/renders"
RENDER_PROMPT_MAX = 4000
RENDER_PROMPT_KEEP = 300          # how much of a prompt the ledger remembers
RENDER_MAX_IMAGES = 3             # xAI's widely documented limit (one page says 5)
RENDER_INPUT_USD = 0.01
RENDER_OUTPUT_USD_EST = 0.06      # seen Nov 5 at default quality; the real cost comes back in usage
RENDER_2K_EXTRA_USD_EST = 0.02    # list-price gap between 1k and 2k medium (reseller tables); an estimate
TICKS_PER_USD = 10_000_000_000    # usage.cost_in_usd_ticks: the $0.08 test came back as 800000000
RENDER_QUOTE_TTL_S = 600
RENDER_PENDING_DAYS = 7           # unreviewed renders are deleted at xAI after this
RENDER_KEEP_CLOSED = 30           # rejected/withheld/failed entries kept as spend history
RENDER_ITEMS_MAX = 260
RENDER_HTTP_UA = "kira-cube-bridge/1"
RENDER_OPEN = ("pending", "approved", "filed")
RENDER_CLOSED = ("rejected", "withheld", "failed", "unknown", "unsaved")

_render_lock = threading.Lock()
_quotes = {}       # quote id -> what was priced; memory only, a quote is a 10-minute promise
_ref_cache = {}    # reference file id -> bytes, so references are fetched once per boot


class _XaiError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"xAI {code}")
        self.code, self.detail = code, detail


def _render_parse(raw):
    r = _json(raw) or {}
    items = r.get("items") if isinstance(r.get("items"), list) else []
    return {"day": r.get("day") if isinstance(r.get("day"), str) else "",
            "spent": r.get("spent") if isinstance(r.get("spent"), int) else 0,
            "count": r.get("count") if isinstance(r.get("count"), int) else 0,
            "items": [dict(i) for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)]}


def _today():
    return datetime.now(CHICAGO).strftime("%Y-%m-%d")


def _usd(ticks):
    return ticks / TICKS_PER_USD


def _render_now():
    """The ledger as it stands (a copy), rolled over to today."""
    if not _den_ready_blocking():
        raise ToolError("Couldn't reach the Cube's MQTT broker to read the render ledger; nothing was spent.")
    r = _den.get("renders") or {"day": "", "spent": 0, "count": 0, "items": []}
    r = {"day": r["day"], "spent": r["spent"], "count": r["count"], "items": [dict(i) for i in r["items"]]}
    if r["day"] != _today():
        r.update(day=_today(), spent=0, count=0)
    return r


def _render_save(r):
    _send(RENDER_TOPIC, json.dumps(r, separators=(",", ":")))
    _den["renders"] = r


def _render_prune(r, now):
    """Drop stale entries; returns xAI file ids to delete (after the ledger is saved)."""
    doomed, keep = [], []
    for i in r["items"]:
        if i.get("status") == "pending" and now - (i.get("created") or now) > RENDER_PENDING_DAYS * 86400:
            doomed.append(i)
        else:
            keep.append(i)
    closed = [i for i in keep if i.get("status") in RENDER_CLOSED]
    old = closed[:-RENDER_KEEP_CLOSED] if len(closed) > RENDER_KEEP_CLOSED else []
    r["items"] = [i for i in keep if not any(i is o for o in old)]
    return [i["file_id"] for i in doomed + old if i.get("file_id")]


def _render_find(items, ref, statuses=RENDER_OPEN):
    """A stored render by id, or by the closet name it was approved under."""
    want = (ref or "").strip()
    for i in reversed(items):
        if i.get("id") == want and i.get("status") in statuses:
            return i
    name = _clean_line(want, DEN_LOOK_NAME_MAX).casefold()
    for i in reversed(items):
        if name and (i.get("look") or "").casefold() == name and i.get("status") in statuses:
            return i
    return None


def _xai(method, path, body=None, content_type="application/json", timeout=60, raw=False):
    if not XAI_KEY:
        raise ToolError("Renders aren't set up yet: XAI_API_KEY is missing on the server.")
    headers = {"Authorization": f"Bearer {XAI_KEY}", "User-Agent": RENDER_HTTP_UA}
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(XAI_BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise _XaiError(e.code, " ".join(detail.split())) from None
    if raw:
        return data
    return json.loads(data) if data else {}


def _xai_reason(e):
    hint = {401: "xAI refused the key (check XAI_API_KEY)",
            403: "xAI refused the request (the key needs image and files permissions)",
            402: "xAI says the prepaid credit is used up",
            429: "xAI's rate limit (the key allows only a few requests a minute); wait a minute"}
    return f"{hint.get(e.code, f'xAI error {e.code}')}: {e.detail}" if e.detail else hint.get(e.code, f"xAI error {e.code}")


def _fetch_public(url, timeout=60):
    """Download xAI's temporary image link. No key: it is a different host."""
    if not url.startswith("https://"):
        raise ToolError("xAI returned an image link that isn't https; not fetching it.")
    req = urllib.request.Request(url, headers={"User-Agent": RENDER_HTTP_UA})
    last = None
    for _ in range(3):  # retrying the download is free; retrying the render is not
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last = e
            time.sleep(1)
    raise last


def _file_get(fid):
    return _xai("GET", f"/files/{urllib.parse.quote(fid, safe='')}/content", raw=True)


def _file_put(data, filename, mime):
    boundary = "kira" + secrets.token_hex(12)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    r = _xai("POST", "/files", body, f"multipart/form-data; boundary={boundary}", timeout=120)
    fid = r.get("id") if isinstance(r, dict) else None
    if not isinstance(fid, str) or not fid:
        raise ToolError("xAI's Files API didn't return a file id.")
    return fid


def _file_del(fid):
    try:
        _xai("DELETE", f"/files/{urllib.parse.quote(fid, safe='')}", timeout=30)
    except Exception:
        pass  # best effort; a leftover file costs nothing to spend


def _mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _data_uri(data):
    return f"data:{_mime(data)};base64," + base64.b64encode(data).decode()


def _render_output_est():
    return RENDER_OUTPUT_USD_EST + (RENDER_2K_EXTRA_USD_EST if XAI_RESOLUTION == "2k" else 0)


def _render_ready():
    if not XAI_KEY:
        raise ToolError("Renders aren't set up yet: XAI_API_KEY is missing on the server.")
    if not XAI_REF_FILES:
        raise ToolError("Renders aren't set up yet: XAI_REF_FILES (the reference image file ids) is missing.")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def render_quote(prompt: str, edit_of: str | None = None) -> str:
    """Price a Grok Imagine render of Kira. Free: nothing is sent to xAI. Nick's two
    reference images are always included, so her face and body stay consistent.
    prompt: the full image prompt (the scene, outfit, pose; or, for an edit, the change).
    edit_of: optional; a render id (from render_image) or a closet look name that has a
    stored render, to edit that image instead of starting fresh ("same outfit, seen from
    the back"). Looks rendered in the Grok app can't be edited here.
    Returns a quote id good for 10 minutes. Show Nick the price and wait for his yes to
    this render before calling render_image."""
    _render_ready()
    p = (prompt or "").strip()
    if not p:
        raise ToolError("The prompt is empty.")
    if len(p) > RENDER_PROMPT_MAX:
        raise ToolError(f"The prompt is too long ({len(p)} characters; max {RENDER_PROMPT_MAX}).")
    r = _render_now()
    target = None
    if edit_of:
        target = _render_find(r["items"], edit_of)
        if not target or not target.get("file_id"):
            raise ToolError(f"No stored render for {edit_of!r}. Only renders made with render_image "
                            "can be edited here; Grok-app renders can't yet.")
    n = len(XAI_REF_FILES) + (1 if target else 0)
    if n > RENDER_MAX_IMAGES:
        raise ToolError(f"That's {n} input images; xAI takes at most {RENDER_MAX_IMAGES}.")
    est = round(n * RENDER_INPUT_USD + _render_output_est(), 2)
    left = RENDER_DAILY_CAP - _usd(r["spent"])
    if est > left + 1e-9:
        raise ToolError(f"That would pass today's ${RENDER_DAILY_CAP:.2f} render cap "
                        f"(${max(left, 0):.2f} left). Nothing was spent.")
    now = time.time()
    for k in [k for k, v in _quotes.items() if v["expires"] < now]:
        del _quotes[k]
    qid = "q" + secrets.token_hex(3)
    _quotes[qid] = {"prompt": p, "est": est, "expires": now + RENDER_QUOTE_TTL_S,
                    "target_id": target["id"] if target else None,
                    "target_file": target["file_id"] if target else None}
    what = f"an edit of {target.get('look') or target['id']}" if target else "a new render"
    return (f"Quote {qid}: {what}, about ${est:.2f} ({n} input images + 1 output). "
            f"${max(left, 0):.2f} of today's ${RENDER_DAILY_CAP:.2f} left. Billed even if xAI's "
            f"moderation withholds the image. Good for 10 minutes. Call render_image with "
            f"quote_id={qid} only after Nick says yes to this.")


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=False, openWorldHint=True))
def render_image(quote_id: str):
    """SPENDS MONEY. Runs a render priced by render_quote. Call it only after Nick has said
    yes to that quote in this conversation, once per yes. Never retry it on your own: a
    retry can bill twice. Returns the image, a short-lived link Nick can open, the actual
    cost and a render id. The render waits as 'pending' until render_review."""
    _render_ready()
    q = _quotes.pop((quote_id or "").strip(), None)
    if not q or q["expires"] < time.time():
        raise ToolError("That quote is unknown or expired (quotes last 10 minutes). Get a new "
                        "quote; nothing was spent.")
    est = int(round(q["est"] * TICKS_PER_USD))
    now = int(time.time())
    with _render_lock:
        r = _render_now()
        if _usd(r["spent"] + est) > RENDER_DAILY_CAP + 1e-9:
            raise ToolError(f"Today's ${RENDER_DAILY_CAP:.2f} render cap would be passed. Nothing was spent.")
        if len(r["items"]) >= RENDER_ITEMS_MAX:
            raise ToolError("The render ledger is full; reject or file some renders first.")
        rid = "r" + secrets.token_hex(3)
        r["items"].append({"id": rid, "status": "running", "created": now, "cost": est,
                           "prompt": q["prompt"][:RENDER_PROMPT_KEEP], "edit_of": q["target_id"],
                           "file_id": None, "look": None, "note": ""})
        r["spent"] += est
        r["count"] += 1
        doomed = _render_prune(r, now)
        _render_save(r)  # the attempt is on the books before xAI is called
    for fid in doomed:
        _file_del(fid)

    sent, cost, status, img, url, file_id, err = False, est, "failed", None, None, None, ""
    try:
        images = []
        if q["target_file"]:
            images.append(_data_uri(_file_get(q["target_file"])))
        for fid in XAI_REF_FILES:
            if fid not in _ref_cache:
                _ref_cache[fid] = _file_get(fid)
            images.append(_data_uri(_ref_cache[fid]))
        payload = {"model": XAI_MODEL, "prompt": q["prompt"],
                   "images": [{"type": "image_url", "url": u} for u in images]}
        if XAI_RESOLUTION:
            payload["resolution"] = XAI_RESOLUTION
        if XAI_QUALITY:
            payload["quality"] = XAI_QUALITY
        body = json.dumps(payload).encode()
        sent = True
        resp = _xai("POST", "/images/edits", body, timeout=240)
        ticks = (resp.get("usage") or {}).get("cost_in_usd_ticks") if isinstance(resp, dict) else None
        if isinstance(ticks, int) and ticks >= 0:
            cost = ticks
        data = resp.get("data") if isinstance(resp, dict) else None
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        url = first.get("url") if isinstance(first.get("url"), str) else None
        if first.get("b64_json"):
            img = base64.b64decode(first["b64_json"])
        elif url:
            img = _fetch_public(url)
        if not img:
            status = "withheld"
        else:
            status = "pending"
            try:
                file_id = _file_put(img, f"{rid}.{_mime(img).split('/')[1]}", _mime(img))
            except Exception as e:
                status, err = "unsaved", (_xai_reason(e) if isinstance(e, _XaiError) else str(e))
    except _XaiError as e:
        err = _xai_reason(e)
        status = "failed"
        if not sent:
            cost = 0
    except ToolError as e:
        err, status = str(e), ("unknown" if sent else "failed")
        if not sent:
            cost = 0
    except Exception as e:
        err, status = f"{type(e).__name__}: {e}", ("unknown" if sent else "failed")
        if not sent:
            cost = 0

    ledger_note = ""
    try:
        with _render_lock:
            r = _render_now()
            for i in r["items"]:
                if i["id"] == rid:
                    i.update(status=status, cost=cost, file_id=file_id)
            r["spent"] = max(0, r["spent"] + cost - est)
            _render_save(r)
    except Exception:
        ledger_note = " (Couldn't update the render ledger; today's total may be off.)"

    money = f"${_usd(cost):.2f}"
    if status == "pending":
        link = f" Link (expires soon): {url}" if url else ""
        return [f"Render {rid} done, {money}. It's pending: approve it with render_review "
                f"(and a closet name) or reject it.{link}{ledger_note}",
                Image(data=img, format=_mime(img).split("/")[1])]
    if status == "unsaved":
        link = f" Link (expires soon): {url}" if url else ""
        return [f"Render {rid} done, {money}, but saving it to xAI's file storage failed ({err}), "
                f"so it can't be approved into the closet or edited later. Nick can still save it "
                f"from the link.{link}{ledger_note}",
                Image(data=img, format=_mime(img).split("/")[1])]
    if status == "withheld":
        raise ToolError(f"xAI's moderation withheld render {rid}; it was still billed ({money}). "
                        f"Don't retry the same prompt.{ledger_note}")
    if status == "unknown":
        raise ToolError(f"Render {rid} may or may not have run ({err}). It may have been billed; "
                        f"check the xAI console before trying again. Don't retry on your own.{ledger_note}")
    raise ToolError(f"Render {rid} failed: {err}. "
                    f"{'Nothing was spent.' if cost == 0 else 'It may have been billed.'}{ledger_note}")


@server.tool()
def render_review(render_id: str, decision: Literal["approve", "reject"],
                  name: str | None = None, note: str = "") -> str:
    """Approve or reject a pending render. approve needs name: the look's closet name
    (new, not already in the closet) and optionally note (outfit, pose, vibe), as for
    add_den_look. Approved renders land in the closet the next time the den page is open.
    reject deletes the render from xAI's storage right away."""
    with _render_lock:
        r = _render_now()
        item = _render_find(r["items"], render_id, statuses=("pending",))
        if not item or item["id"] != (render_id or "").strip():
            raise ToolError(f"No pending render {render_id!r}.")
        if decision == "reject":
            fid = item.get("file_id")
            item.update(status="rejected", file_id=None)
            _render_save(r)
            if fid:
                _file_del(fid)
            return f"Rejected {item['id']}; deleted from xAI."
        clean = _clean_line(name or "", DEN_LOOK_NAME_MAX)
        if not clean:
            raise ToolError("Approving needs a closet name for the look.")
        if not item.get("file_id"):
            raise ToolError("That render wasn't saved to xAI's storage, so it can't go to the closet.")
        if _find_look(_closet_now(), clean) or any(
                (i.get("look") or "").casefold() == clean.casefold() and i is not item
                and i.get("status") in ("approved", "filed") for i in r["items"]):
            raise ToolError(f"A look named {clean!r} already exists; pick another name.")
        item.update(status="approved", look=clean, note=_clean_line(note or "", DEN_LOOK_NOTE_MAX))
        _render_save(r)
    return f"Approved {item['id']} as {clean!r}. It goes into the closet the next time the den page is open."


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def render_inbox() -> dict:
    """For the den page: approved renders waiting to be saved into the closet
    (renders: [{id, name, note, created}])."""
    r = _render_now()
    return {"renders": [{"id": i["id"], "name": i["look"], "note": i.get("note", ""),
                         "created": i.get("created")}
                        for i in r["items"] if i.get("status") == "approved"]}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def render_fetch(render_id: str) -> dict:
    """For the den page: one approved render's image as base64 ({id, name, note, mime, b64})."""
    r = _render_now()
    item = _render_find(r["items"], render_id, statuses=("approved", "filed"))
    if not item or item["id"] != (render_id or "").strip() or not item.get("file_id"):
        raise ToolError(f"No approved render {render_id!r}.")
    try:
        data = _file_get(item["file_id"])
    except _XaiError as e:
        raise ToolError(_xai_reason(e)) from None
    return {"id": item["id"], "name": item["look"], "note": item.get("note", ""),
            "mime": _mime(data), "b64": base64.b64encode(data).decode()}


@server.tool()
def render_filed(render_id: str) -> str:
    """For the den page: mark an approved render as saved into the closet. Its xAI copy is
    kept so the look can be edited later, until the look is removed from the closet."""
    with _render_lock:
        r = _render_now()
        item = _render_find(r["items"], render_id, statuses=("approved",))
        if not item or item["id"] != (render_id or "").strip():
            raise ToolError(f"No approved render {render_id!r}.")
        item["status"] = "filed"
        _render_save(r)
    return f"{item['look']!r} is filed."


def _render_forget_look(name):
    """When a look leaves the closet, delete its stored render at xAI too. Best effort."""
    try:
        with _render_lock:
            r = _render_now()
            gone = [i for i in r["items"] if (i.get("look") or "").casefold() == name.casefold()
                    and i.get("status") in ("approved", "filed")]
            if not gone:
                return
            r["items"] = [i for i in r["items"] if not any(i is g for g in gone)]
            _render_save(r)
        for i in gone:
            if i.get("file_id"):
                _file_del(i["file_id"])
    except Exception:
        pass  # removing the look already worked; a leftover file is harmless


# ---------------------------------------------------------------------------
# Evening ember (Nick, Oct 18): night should start at 8:30 p.m., but the Cube's own
# clock (in its firmware) still switches to ember at 11 p.m. Until the firmware is
# reflashed, this keeper covers the gap: between EVENING_EMBER_START and the firmware's
# night start, whenever the Cube reports its daytime idle ("rain (schedule)"), it sends
# ember held until the firmware takes over. Chat moods, tasks and off duty are left
# alone; after a quick reaction expires the Cube falls back to rain and the keeper
# re-asserts ember within a minute. Once the firmware switches at 8:30 itself, the Cube
# never reports rain in that window and this does nothing.
# ---------------------------------------------------------------------------
def _hhmm(text, default):
    try:
        h, m = (int(x) for x in str(text).split(":"))
        return h * 60 + m
    except (TypeError, ValueError):
        return default


EVENING_EMBER_START = _hhmm(os.environ.get("EVENING_EMBER_START", "20:30"), 20 * 60 + 30)
FIRMWARE_NIGHT_START = _hhmm(os.environ.get("FIRMWARE_NIGHT_START", "23:00"), 23 * 60)
KEEPER_EVERY_S = 60


def evening_ember_due(state, now_dt):
    """Seconds of ember to send right now, or 0. `state` is the Cube's retained state
    string (e.g. "rain (schedule)"); `now_dt` an aware Chicago datetime."""
    t = now_dt.hour * 60 + now_dt.minute
    if not (EVENING_EMBER_START <= t < FIRMWARE_NIGHT_START):
        return 0
    s = (state or "").strip().lower()
    if not (s.startswith("rain") and "(schedule)" in s):
        return 0
    return max(60, (FIRMWARE_NIGHT_START - t) * 60 - now_dt.second)


def _evening_keeper():
    while True:
        try:
            _den_ready_blocking()
            secs = evening_ember_due(_den["state"], datetime.now(CHICAGO))
            if secs:
                _send("mood", f"ember hold={secs} {_stamp()}")
                _den["state"] = "ember (chat)"  # don't resend before the Cube reports back
        except Exception:
            pass  # broker hiccup; try again next minute
        time.sleep(KEEPER_EVERY_S)


if __name__ == "__main__":
    threading.Thread(target=_evening_keeper, name="evening-ember", daemon=True).start()
    server.run(
        "streamable-http",
        host="0.0.0.0",
        port=PORT,
        streamable_http_path=f"/{SECRET}/mcp",
        stateless_http=True,
        json_response=True,
    )
