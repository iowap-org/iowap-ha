# IOWAP for Home Assistant

**Your Home Assistant becomes a first-class node in your IOWAP cluster.**

[Repository-Layout: one repo, two scanners — the Supervisor app store reads
the `iowap/` app folder, HACS reads `custom_components/iowap/`.]

## What it does

| Direction | What | How |
|-----------|------|-----|
| Cluster → HA | Tasks from IOWAP nodes drive approved home capabilities (lights, climate, scenes, state reads; locks read-only by default) | `iowap/` app: node-daemon + `ha-exec` handler with a fixed capability matrix |
| HA → Cluster | Automations submit tasks into the cluster (e.g. smoke detected → `agent.ai`) | `iowap.submit_task` service → `hassio.app_stdin` → app outbox |
| HA visibility | Relay health/readiness as HA sensors | app-side (v2) |

## Installation

**App (cluster → HA):** Settings → Apps → App Store → ⋮ → Repositories → add
this repository URL. Install **IOWAP Node**, set your `relay_url` in the app
options, start. The node registers on the relay and appears in your relay
dashboard for approval (status: `pending`).

**Integration (HA → cluster):** HACS → ⋮ → Custom repositories → this URL,
category *Integration*. Then Settings → Devices & Services → Add Integration
→ **IOWAP**.

## Security model

- HA core holds **no relay credentials** — the app container holds the only
  relay token and is the single bridge to the relay.
- `ha-exec` enforces a fixed capability matrix: `domain`+`service` are never
  taken from the task payload, entity scope is validated per domain, and a
  per-capability rate limiter throttles runaway automations. Locks stay
  read-only unless you explicitly set `lock_level: write` in the app options.
- Core→app communication is the official `hassio.app_stdin` channel only —
  no network ports opened toward HA core.
- Safety automations stay **local**: never couple a safety behavior to the
  relay being up.

### Capability reference

Published automatically from the `CAPS` table in `iowap/ha-exec.py` (single
source of truth — descriptions and schemas are generated into the node
profile at boot, never hand-maintained). All write capabilities are also
subject to per-domain entity scope (`*_entity_scope` options) and a
per-capability rate limit (`rate_limit_per_min`).

| Capability | Service | Input fields |
|---|---|---|
| `ha.light.on.native` | `light.turn_on` | `entity_id`, `brightness_pct` 0–100, `color_temp_kelvin` 1500–6500, `transition` 0–60 s |
| `ha.light.off.native` | `light.turn_off` | `entity_id`, `transition` |
| `ha.light.toggle.native` | `light.toggle` | `entity_id` |
| `ha.scene.activate.native` | `scene.turn_on` | `entity_id` |
| `ha.climate.set_temperature.native` | `climate.set_temperature` | `entity_id`, `temperature` 5–35 °C |
| `ha.media.play_pause.native` | `media_player.media_play_pause` | `entity_id` |
| `ha.switch.toggle.native` | `switch.toggle` | `entity_id` |
| `ha.fan.toggle.native` | `fan.toggle` | `entity_id` |
| `ha.humidifier.toggle.native` | `humidifier.toggle` | `entity_id` |
| `ha.vacuum.start.native` | `vacuum.start` | `entity_id` |
| `ha.vacuum.return_to_base.native` | `vacuum.return_to_base` | `entity_id` |
| `ha.state.get.native` | state read (any domain) | `entity_id` |
| `ha.lock.lock.native` | `lock.lock` — **only with** `lock_level: write` | `entity_id` |
| `ha.lock.unlock.native` | `lock.unlock` — **only with** `lock_level: write` | `entity_id` |

`lock.open` does not exist in the matrix and is rejected by design — opening
a latch is never available through IOWAP.

## Node status in HA

The app pushes its state into Home Assistant every `status_push_interval`
seconds (app option, default 60) via the Supervisor proxy — HA core never
polls the app and no extra network path is opened:

- `binary_sensor.iowap_node_ready` — `on` while the node-daemon heartbeats
  healthily (status file fresh, heartbeat ok, no auth loop), otherwise `off`
  with a `reason` attribute. Attributes: `node_id`, `last_heartbeat`,
  `active_profile`, `capabilities`, `auth_loop`, `error`.
- `sensor.iowap_node_metrics` — state = number of published capabilities.
  Attributes: `tasks_completed`, `tasks_failed`, `in_flight` and `per_cap`
  (per-capability `calls`, `denied`, `last_call`, `last_outcome` counted by
  the `ha-exec` handler).

## Repository layout

```
iowap-ha/
├── repository.yaml              # Supervisor app-repository manifest
├── hacs.json                    # HACS manifest
├── iowap/                       # HAOS app (node container)
│   ├── config.yaml              # app manifest (options schema, image)
│   ├── Dockerfile, run.sh
│   └── ha-exec.py               # capability boundary handler
└── custom_components/iowap/     # thin custom integration
    ├── manifest.json, config_flow.py, __init__.py
    └── services.yaml            # iowap.submit_task
```

## Links

- [iowap](https://github.com/iowap-org/iowap) — the story + architecture
- [iowap-node](https://github.com/iowap-org/iowap-node) — node framework
- [iowap-server](https://github.com/iowap-org/iowap-server) — relay server