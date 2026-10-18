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

`GET /den/state` is a public, read-only feed for Kira's den page (nichlittle.github.io/den/). It returns the Cube's current mood and nothing else:

```json
{"mood": "rain", "source": "schedule", "online": true, "updated": "2026-10-18T10:45:00-05:00"}
```

`mood` is `rain`, `violet`, `blue`, `red`, `ember` or `off_duty` (or `null` if the Cube hasn't reported yet); `source` is `schedule`, `chat`, `task` or `off_duty`. `updated` is when the bridge last saw the state change. No tap data, device details or secrets ever go in it. Browsers may read it only from `https://nichlittle.github.io` (override with the optional `DEN_ORIGIN` variable), and responses are never cached.

**Tools:** `set_mood`, `clear_mood`, `task_running`, `set_brightness`, and `cube_status`.

**Deploy (Render):** create a new Web Service from this repo with Runtime set to Docker (the root `Dockerfile` is used by default) and the Free instance type. Add the variables above. Render sets `PORT` itself.
