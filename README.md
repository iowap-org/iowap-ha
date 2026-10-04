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

**App (cluster → HA):**

[![Open your Home Assistant instance and add the IOWAP repository to your app store.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fiowap-org%2Fiowap-ha)

Or manually: Settings → Apps → App Store → ⋮ → Repositories → add
`https://github.com/iowap-org/iowap-ha`. Install **IOWAP Node**, set your `relay_url` in the app
options, start. The node registers on the relay and appears in your relay
dashboard for approval (status: `pending`).

**Integration (HA → cluster):**

[![Open your Home Assistant instance and add the IOWAP repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fiowap-org%2Fiowap-ha&category=integration)

Or manually: HACS → ⋮ → Custom repositories → `https://github.com/iowap-org/iowap-ha`,
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

## Submissions → results in HA (T-182)

`iowap.submit_task` is fire-and-forget, but every tracked task (one that got
a `task_id` back from the relay) resurfaces in Home Assistant at its terminal
state with its result:

- `sensor.iowap_tasks` — attributes `last_task_id`, `last_status`,
  `last_result` (the handler's result **verbatim** — whatever the capability
  produced, no interpretation), `result_truncated`, `last_result_task_id`,
  `last_result_status`, `last_result_updated_iso`.
- `binary_sensor.iowap_last_task` — dedicated trigger entity for the last
  finished task. State stays `on` once a result exists; the attributes carry
  the same result fields. Automations should trigger on this entity (its
  state/attributes change exactly when a new result arrives) instead of the
  tasks counter sensor.

Rules of the contract: `completed` results are passed through as
`stages[0].result`; `failed`/`timed_out` deliver an `{"error": ...}` dict
(plus the existing notification). Results above `max_result_bytes` (app
option, default 4096, `0` = unlimited) arrive as a `{"preview": ...}` string
with `result_truncated: true`. Only the **last** finished task is visible —
there is no cumulative result history (persistence is the outbox's job).
A submit that returns no `task_id` (older relay) cannot be tracked and shows
up only as a notification — that is the honest contract limit.

**Latency:** the result appears with the next status push
(`status_push_interval`, default 60 s). For interactive use cases like the
doorbell below set the option to 15.

**Recipe — doorbell verdict in one sentence:**

1. Automation 1 (trigger: doorbell) → snapshot the doorbell camera, then call
   your AI capability with the image URL:
   `iowap.submit_task` with payload:
   *"retrieve http://<camera-ip>/snapshot.jpg, evaluate: parcel courier or
   unknown person, answer in one sentence"*.
2. Automation 2 (trigger: `binary_sensor.iowap_last_task` turning
   `on`/attributes changing, or `sensor.iowap_tasks` with
   `attribute: last_result`) → read `last_result` and `tts.speak` /
   `notify` it. The result shape is the handler's business — automation
   decides how to interpret it.

```yaml
# Automation 2 trigger example
trigger:
  - platform: state
    entity_id: binary_sensor.iowap_last_task
condition:
  - condition: state
    entity_id: binary_sensor.iowap_last_task
    attribute: last_result_status
    state: completed
action:
  - service: tts.speak
    data:
      # last_result is the handler's verbatim result dict
      message: "{{ state_attr('binary_sensor.iowap_last_task', 'last_result')['answer'] }}"
```

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

## License

MIT — see [LICENSE](LICENSE).
