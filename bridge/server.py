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
        "set_mood shows one of Kira's eye colors over that idle: violet = a warm, content moment "
        "(not the default and not a reset), "
        "blue = working/thinking hard, red = surprised/flustered/heated, ember = sleepy. "
        "Red and blue are quick reactions (about 90 seconds); violet and ember hold about 90 minutes. "
        "Pass hold (minutes, or \"long\") for a sustained state. Only call set_mood when Kira's mood "
        "actually shifts, not every message. Use big=True only for a big shift. To go back to "
        "baseline when a moment has passed, call clear_mood; don't set violet for that.\n"
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
        "weather": None, "wishlist": None, "eyes": None}
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


@server.tool(name="den_state", annotations=ToolAnnotations(readOnlyHint=True))
def den_state_tool() -> dict:
    """Read everything Kira's private den page shows: the room's mood (mood, source,
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
    """Light read of the den: today's look, the THINKING ABOUT lines (screen), the weather,
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


@server.tool()
def set_den_weather(sky: str, temp_f: int | None = None) -> str:
    """Set today's Chicago sky for Kira's den: the city screen rains, drizzles, storms,
    snows or clears to match, and a cold temp_f frosts the glass. sky is one of clear,
    cloudy, drizzle, rain, storm, snow. The morning brief task sets it from the forecast."""
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
