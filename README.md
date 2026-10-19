# Kira Cube bridge

A small remote MCP server that lets Kira (Claude, through a custom connector) set the mood on the Kira Cube, a 5×5 LED light on Nick's desk. The request path is: Claude → this server (HTTPS) → EMQX Cloud (MQTT over TLS) → the cube.

There are no secrets in this repo. Everything private comes from environment variables set on the host:

| Variable | What |
|---|---|
| `MQTT_HOST` | the EMQX deployment address (`…emqxsl.com`) |
| `MQTT_PORT` | `8883` |
| `MQTT_USER` / `MQTT_PASS` | the bridge's EMQX login (`kira-bridge`) |
| `TOPIC_PREFIX` | the same prefix as the cube's `config.py` |
| `CUBE_SECRET` | a long random string; it is the lock on the URL |

The connector URL is `https://<host>/<CUBE_SECRET>/mcp`. Treat it like a password. `GET /healthz` is a public "I'm awake" check for the keep-warm pinger.

**Tools:** `set_mood`, `clear_mood`, `task_running`, `set_brightness`, `cube_status`, plus the den tools: `den_state` (read everything the den page shows), `set_den_screen` (the THINKING ABOUT lines), `set_den_look` (today's look), `add_den_look` / `remove_den_look` (the closet of rendered looks) `set_den_brief` (the morning brief), `set_den_weather` (today's sky for the room) and the outfit Wishlist: `add_den_suggestion` (Kira pitches a look with its Grok prompt) and `resolve_den_suggestion` (Nick cuts it or marks it rendered; cut names are never pitched again). The den's content is kept as retained messages under `den/` on the broker, so it survives restarts.

**Deploy (Render):** create a new Web Service from this repo with Runtime set to Docker (the root `Dockerfile` is used by default) and the Free instance type. Add the variables above. Render sets `PORT` itself.

**Evening ember:** night should start at 8:30 p.m. but the Cube's firmware still switches at 11. Until it's reflashed, a background thread covers 8:30 to 11: whenever the Cube reports its daytime rain, it sends ember held until 11. Optional env vars: `EVENING_EMBER_START` (default `20:30`) and `FIRMWARE_NIGHT_START` (default `23:00`). Once the firmware matches, it does nothing.
