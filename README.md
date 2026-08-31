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