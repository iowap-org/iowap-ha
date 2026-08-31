#!/usr/bin/env python3
"""T-169 reference handler: executes ONE HA capability per invocation.

Lives in the IOWAP HA app container. The node-daemon (iowap-node) runs this
via handler_runner: payload JSON on stdin, result JSON on stdout, exit 0 ok.
Which HA call this run performs is looked up from RELAY_CAPABILITY via the
CAPS table below — the table is the security boundary: domain+service are
NEVER taken from the task payload.

Auth: Supervisor proxy bearer token from the environment (SUPERVISOR_TOKEN
inside HAOS apps). Scope/rate config: /data/options.json (light_entity_scope,
rate_limit_per_min, lock_level) — set by the thin Integration's options UI.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")

# The fixed capability matrix. domain+service: NOT payload-controlled.
# fields:  payload keys allowed through to HA (validated types/regexes)
CAPS = {
    "ha.light.on.native":              {"domain": "light",   "service": "turn_on",       "fields": ("entity_id", "brightness_pct", "color_temp_kelvin", "transition")},
    "ha.light.off.native":             {"domain": "light",   "service": "turn_off",      "fields": ("entity_id", "transition")},
    "ha.light.toggle.native":          {"domain": "light",   "service": "toggle",        "fields": ("entity_id",)},
    "ha.scene.activate.native":        {"domain": "scene",   "service": "turn_on",       "fields": ("entity_id",)},
    "ha.climate.set_temperature.native": {"domain": "climate", "service": "set_temperature", "fields": ("entity_id", "temperature")},
    "ha.media.play_pause.native":      {"domain": "media_player", "service": "media_play_pause", "fields": ("entity_id",)},
    "ha.switch.toggle.native":         {"domain": "switch",  "service": "toggle",        "fields": ("entity_id",)},
    "ha.fan.toggle.native":            {"domain": "fan",     "service": "toggle",        "fields": ("entity_id",)},
    "ha.humidifier.toggle.native":     {"domain": "humidifier", "service": "toggle",     "fields": ("entity_id",)},
    "ha.vacuum.start.native":          {"domain": "vacuum",  "service": "start",         "fields": ("entity_id",)},
    "ha.vacuum.return_to_base.native": {"domain": "vacuum",  "service": "return_to_base", "fields": ("entity_id",)},
    "ha.state.get.native":             {"domain": "*",       "service": "GET_STATE",     "fields": ("entity_id",)},  # read-only, all domains
    "ha.lock.lock.native":             {"domain": "lock",    "service": "lock",          "fields": ("entity_id",)},   # write-gated: lock_level
    "ha.lock.unlock.native":           {"domain": "lock",    "service": "unlock",        "fields": ("entity_id",)},   # write-gated: lock_level; 'open' deliberately absent
}

WRITE_CAPS = {k for k, v in CAPS.items() if v["service"] not in ("GET_STATE",)}


def load_cfg() -> dict:
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except Exception:
        return {}  # safe defaults below


def scope_allowed(cfg: dict, cfg_key: str, entity_id: str) -> bool:
    pattern = cfg.get(cfg_key, "*")
    return fnmatch.fnmatch(entity_id, pattern)


def rate_ok(cfg: dict, cap: str) -> tuple[bool, str]:
    # Token bucket per capability; degrades to "allow" if state dir unwritable
    # (rate limit is defense-in-depth, must not break handler on odd setups).
    limit = int(cfg.get("rate_limit_per_min", 20))
    try:
        sdir = Path("/data/handler_state"); sdir.mkdir(parents=True, exist_ok=True)
        f = sdir / f"{cap.replace('.', '_')}.times"
        now = time.time()
        try:
            stamps = [float(x) for x in f.read_text().split() if x]
        except Exception:
            stamps = []
        stamps = [t for t in stamps if now - t < 60]
        if len(stamps) >= limit:
            return False, f"rate limit {limit}/min exceeded for {cap}"
        stamps.append(now)
        f.write_text(" ".join(map(str, stamps)))
    except OSError as e:  # unwritable state dir: log-and-allow, don't kill the stage
        print(f"rate-limit state unavailable, allowing without limit: {e}", file=sys.stderr)
    return True, ""


def caps_json() -> None:
    """Emit the CAPS table as JSON for the run.sh profile bootstrap.

    One source of truth: gen_profile.py mirrors this into node.yaml, so the
    published capability list can never drift from the security boundary.
    """
    caps = [
        {"name": name,
         "version": "1.0.0",
         "type": "tool",
         "description": f"HA {spec['domain']}.{spec['service']}"
                        if spec["service"] != "GET_STATE"
                        else f"HA state read ({spec['domain']})",
         "input_schema": {
             "type": "object",
             "required": ["entity_id"],
             "properties": {k: {"type": "string"} if k == "entity_id"
                            else {"type": ["number", "string"]}
                            for k in spec["fields"]},
         }}
        for name, spec in CAPS.items()
    ]
    print(json.dumps(caps, indent=2))


def main() -> int:
    if "--caps-json" in sys.argv:
        caps_json()
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid payload JSON: {e}"}))
        return 2

    cap = os.environ.get("RELAY_CAPABILITY", "")
    spec = CAPS.get(cap)
    if not spec:
        print(json.dumps({"error": f"unknown capability {cap!r}"}))
        return 2

    entity_id = str(payload.get("entity_id", "")).strip()
    if not entity_id or entity_id.count(".") != 1 or any(c in entity_id for c in " ;/&$`\x00"):
        print(json.dumps({"error": "invalid entity_id"}))
        return 2

    # domain-consistency: entity prefix must match capability's domain
    # (GET_STATE = "*", alles andere: cross-domain wie light.on→lock.x ist invalid)
    ent_domain = entity_id.split(".")[0]
    if spec["domain"] != "*" and ent_domain != spec["domain"]:
        print(json.dumps({"error": f"capability {cap} is for domain {spec['domain']}, got {ent_domain} entity"}))
        return 2

    cfg = load_cfg()
    cfg_key = ent_domain + "_entity_scope"
    if not scope_allowed(cfg, cfg_key, entity_id):
        print(json.dumps({"error": f"entity {entity_id} outside configured scope"}))
        return 3

    # lock write-gate: read-only unless integration config explicitly upgraded
    if spec["domain"] == "lock" and cfg.get("lock_level", "read") != "write":
        print(json.dumps({"error": "lock is read-only (integration lock.level=read) — use ha.state.get.native for reading"}))
        return 3
    if spec["service"] == "open":  # latch — never via relay, any level
        print(json.dumps({"error": "lock.open is local-only by design"}))
        return 3

    ok, msg = rate_ok(cfg, cap)
    if not ok:
        print(json.dumps({"error": msg}))
        return 3

    # build minimal HA service payload: whitelist passthrough only
    data = {}
    if spec["service"] != "GET_STATE":
        for k in spec["fields"]:
            if k in payload and k != "entity_id":
                data[k] = payload[k]
        data["entity_id"] = entity_id

    ha_url = os.environ.get("IOWAP_HA_URL", "http://supervisor/core/api")  # override for local tests
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    body = json.dumps({"entity_id": entity_id, **data}) if spec["service"] != "GET_STATE" else None
    url = (f"{ha_url}/states/{entity_id}" if spec["service"] == "GET_STATE"
           else f"{ha_url}/services/{spec['domain']}/{spec['service']}")

    # curl instead of httpx: zero extra deps in the app base image, blocking is fine here
    import subprocess
    cmd = ["curl", "-sS", "-m", "10", "-w", "\\n%{http_code}",
           "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
           "-X", "GET" if body is None else "POST", "-d", body or "", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "HA call timeout"}))
        return 1
    if out.count("\n") < 1:
        print(json.dumps({"error": "no response from HA proxy"}))
        return 1
    body_out, code = out.rsplit("\n", 1)
    if code.strip() not in ("200", "201"):
        print(json.dumps({"error": f"HA returned HTTP {code}", "detail": body_out[:300]}))
        return 1

    try:
        state = json.loads(body_out) if body_out and body_out.strip() != "[]" else {}
    except json.JSONDecodeError:
        state = {"raw": body_out[:300]}
    if isinstance(state, list):  # service calls return a LIST of affected states — take the first
        state = state[0] if state else {}

    if spec["service"] == "GET_STATE":
        result = {"ok": True, "entity_id": entity_id,
                  "state": state.get("state"), "attributes": state.get("attributes", {}),
                  "last_changed": state.get("last_changed")}
    else:
        result = {"ok": True, "entity_id": entity_id, "called": f"{spec['domain']}.{spec['service']}",
                  "state_after": state.get("state", "unknown")}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())