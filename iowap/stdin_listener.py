#!/usr/bin/env python3
"""IOWAP app stdin-listener (T-169 design, ref T-169-ha-node-design.md L.186).

Consumes lines on stdin (each a JSON envelope written by HA core via the
official ``hassio.app_stdin`` service), validates them, and appends them
crash-safe to /data/outbox.jsonl. A drain loop then submits each envelope to
the relay via RelayClient.submit_simple_task() — the same library the daemon
uses, with the same runtime token, so retry/backoff semantics apply.

On first boot (no ~/.relay/iowap-agent.json) it registers the node with the
relay via POST /relay/v2/auth/register, retrying until the relay is reachable.

Fail-behavior: app_stdin is fire-and-forget (no error return to the caller).
Visibility instead: outbox.jsonl line count == pending count, plus a
persistent_notification via the Supervisor proxy on final failure.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("stdin-listener")
DATA = Path("/data")
OUTBOX = DATA / "outbox.jsonl"
# Stable node identity across container rebuilds (HOSTNAME is not stable).
NODE_NAME = "ha-app-node"
MAX_TRIES = 32                # per-envelope drain budget before final failure
DRAIN_INTERVAL = 5.0
# T-172b: telemetry push — worker_status.json (daemon) + metrics.json
# (ha-exec) are mirrored into HA as entities via the Supervisor proxy.
STATUS_PATH = Path(
    os.environ.get("IOWAP_STATUS_PATH", str(Path(os.environ.get("HOME", "/data")) / ".relay" / "worker_status.json"))
)
METRICS_PATH = Path(
    os.environ.get("IOWAP_METRICS_PATH", "/data/handler_state/metrics.json")
)
STATUS_MAX_AGE_S = 45  # heartbeat_interval 8s → a healthy daemon never goes stale
DEFAULT_STATUS_PUSH_INTERVAL = 60

VALID_KINDS = {"submit_task", "set_domain_states"}
# same env override as ha-exec.py (tests); app default /data (persistent)
DOMAIN_STATES_PATH = Path(
    os.environ.get("IOWAP_DOMAIN_STATES_PATH", "/data/domain_states.json")
)
HA_EXEC = "/usr/local/bin/ha-exec.py"
GEN_PROFILE = "/usr/local/bin/gen_profile.py"
# $CONFIG_DIR — run.sh starts node-daemon with WorkingDirectory=$CONFIG_DIR,
# so the daemon's ACTIVE_PATH (default ~/.relay/node.yaml) lives here.
CONFIG_DIR = Path(os.environ.get("IOWAP_CONFIG_DIR", "/data/.relay"))
DOMAIN_MAP_KEYS = ("light", "scene", "climate", "media_player", "switch",
                   "fan", "humidifier", "vacuum", "lock")


def _apply_domain_states(states: dict) -> dict:
    """T-173: persist domain states and regenerate the live profile.

    1. ha-exec.py --write-domain-states  (atomic persist, whitelist-clean)
    2. ha-exec.py --caps-json --filter-states | gen_profile.py > node.yaml
       (same pipeline as run.sh bootstrap, minus caps not allowed by states)
    3. SIGHUP the node-daemon → invalidates its profile cache → next
       heartbeat republishes the reduced capability list to the relay.

    Returns the applied (whitelist-cleaned) states for echo-back.
    """
    import signal as _signal

    r = subprocess.run(
        ["python3", HA_EXEC, "--write-domain-states"],
        input=json.dumps(states), capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(f"write-domain-states failed: {r.stderr.strip()}")
    applied = json.loads(r.stdout or "{}")

    r = subprocess.run(
        ["python3", HA_EXEC, "--caps-json", "--filter-states"],
        capture_output=True, text=True, timeout=10, check=True,
    )
    profile = subprocess.run(
        ["python3", GEN_PROFILE],
        input=r.stdout, capture_output=True, text=True, timeout=10, check=True,
    )
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_DIR / "node.yaml.tmp"
    tmp.write_text(profile.stdout, encoding="utf-8")
    tmp.replace(CONFIG_DIR / "node.yaml")  # atomic — daemon cache keys on mtime

    # kick the daemon(s) so the mtime-cached profile is re-read at once.
    # SIGHUP is idempotent for the daemon (cache invalidation only), so
    # signalling every match is fine — but never signal ourselves.
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["pgrep", "-f", "node-daemon|node_cli.*daemon"], capture_output=True,
            text=True, timeout=5,
        ).stdout.split()
        pids = [int(p) for p in out if p.isdigit() and int(p) != os.getpid()]
    except (ValueError, subprocess.SubprocessError):
        pass
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGHUP)
            LOG.info("SIGHUP → node-daemon (pid %s): profile reload", pid)
        except OSError as exc:
            LOG.warning("SIGHUP to daemon %s failed (heartbeat still republishes): %s", pid, exc)
    return applied


def _load_config() -> dict[str, Any]:
    cfg_path = DATA / ".relay" / "relay_config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.setdefault("request_timeout", 10)
    return cfg


def _load_caps() -> list[dict[str, Any]]:
    """Capability list from ha-exec.py's CAPS table (single source of truth)."""
    out = subprocess.run(
        ["python3", "/usr/local/bin/ha-exec.py", "--caps-json", "--filter-states"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    return json.loads(out)


def _validate(envelope: dict) -> str | None:
    """Return an error string if the envelope is not acceptable."""
    if envelope.get("kind") not in VALID_KINDS:
        return f"unknown kind {envelope.get('kind')!r}"
    # set_domain_states is handled locally (no outbox, no relay round-trip)
    if envelope.get("kind") == "set_domain_states":
        states = envelope.get("states")
        if not isinstance(states, dict):
            return "states must be an object of domain → on|readonly|off"
        bad = {d: v for d, v in states.items()
               if d not in DOMAIN_MAP_KEYS or v not in ("on", "readonly", "off")}
        if bad:
            return f"invalid domain states: {bad}"
        return None
    cap = envelope.get("capability")
    if not isinstance(cap, str) or not cap:
        return "capability must be a non-empty string"
    if not isinstance(envelope.get("payload"), dict):
        return "payload must be an object"
    prio = envelope.get("priority", 5)
    if not isinstance(prio, int) or isinstance(prio, bool) or not 1 <= prio <= 9:
        return "priority must be int 1..9"
    return None


def _append_outbox(envelope: dict) -> None:
    line = json.dumps(envelope, separators=(",", ":"))
    with OUTBOX.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())  # crash-safe: fsync per line


def _notify(text: str) -> None:
    """persistent_notification via the Supervisor proxy (best effort)."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        LOG.warning("no SUPERVISOR_TOKEN, cannot notify: %s", text)
        return
    try:
        import httpx

        httpx.post(
            "http://supervisor/core/api/services/persistent_notification/create",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": text, "title": "IOWAP"},
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("notification failed: %s", exc)


# --- T-172b: telemetry push (app → HA core via Supervisor proxy) -----------

def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _compute_state() -> dict:
    """Merge worker_status.json + metrics.json into the HA payload.

    Returns {} when there is nothing to push yet (daemon not started).
    """
    status = _read_json(STATUS_PATH)
    if status is None:
        return {}

    ready = False
    reason = "unknown"
    try:
        age = time.time() - STATUS_PATH.stat().st_mtime
        auth_loop = bool(status.get("auth_loop"))
        hb_ok = status.get("heartbeat_status") in ("ok", "approved", "claimed")
        ready = age <= STATUS_MAX_AGE_S and hb_ok and not auth_loop
        if not ready:
            if age > STATUS_MAX_AGE_S:
                reason = f"status stale ({int(age)}s old)"
            elif auth_loop:
                reason = "auth loop (token invalid)"
            else:
                reason = f"heartbeat {status.get('heartbeat_status')!r}"
    except OSError:
        reason = "status unreadable"
        ready = False

    per_cap = _read_json(METRICS_PATH) or {}
    return {
        "ready": ready,
        "reason": reason if not ready else "",
        "node_id": status.get("node_id"),
        "last_heartbeat": status.get("last_heartbeat"),
        "active_profile": status.get("active_profile"),
        "auth_loop": status.get("auth_loop", False),
        "error": status.get("error") or "",
        "capabilities": len(status.get("capabilities") or []),
        "tasks_completed": status.get("tasks_completed", 0),
        "tasks_failed": status.get("tasks_failed", 0),
        "in_flight": len(status.get("in_flight") or {}),
        "per_cap": per_cap,
    }


def _push_telemetry(payload: dict, token: str) -> bool:
    """Create/update the two HA entities. Returns True on full success."""
    import httpx

    ok = True
    r = httpx.post(
        "http://supervisor/core/api/states/binary_sensor.iowap_node_ready",
        headers={"Authorization": f"Bearer {token}"},
        json={"state": "on" if payload["ready"] else "off",
              "attributes": {"friendly_name": "IOWAP Node Ready",
                             "icon": "mdi:cloud-check",
                             "reason": payload["reason"],
                             "node_id": payload["node_id"],
                             "last_heartbeat": payload["last_heartbeat"],
                             "active_profile": payload["active_profile"],
                             "auth_loop": payload["auth_loop"],
                             "error": payload["error"],
                             "capabilities": payload["capabilities"]}},
        timeout=5,
    )
    ok &= r.status_code in (200, 201)
    if r.status_code not in (200, 201):
        LOG.warning("ready sensor push failed: HTTP %s", r.status_code)

    r = httpx.post(
        "http://supervisor/core/api/states/sensor.iowap_node_metrics",
        headers={"Authorization": f"Bearer {token}"},
        json={"state": payload["capabilities"],
              "attributes": {"friendly_name": "IOWAP Node Metrics",
                             "icon": "mdi:chart-box",
                             "unit_of_measurement": "capabilities",
                             "tasks_completed": payload["tasks_completed"],
                             "tasks_failed": payload["tasks_failed"],
                             "in_flight": payload["in_flight"],
                             "per_cap": payload["per_cap"]}},
        timeout=5,
    )
    ok &= r.status_code in (200, 201)
    if r.status_code not in (200, 201):
        LOG.warning("metrics sensor push failed: HTTP %s", r.status_code)
    return ok


def _telemetry_loop() -> None:
    interval = DEFAULT_STATUS_PUSH_INTERVAL
    try:
        opts = json.loads((DATA / "options.json").read_text())
        interval = int(opts.get("status_push_interval") or DEFAULT_STATUS_PUSH_INTERVAL)
    except Exception:
        pass
    LOG.info("telemetry push every %ss", interval)
    while True:
        try:
            token = os.environ.get("SUPERVISOR_TOKEN", "")
            payload = _compute_state()
            if payload and token:
                _push_telemetry(payload, token)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("telemetry push error: %s", exc)
        for _ in range(max(1, interval)):
            time.sleep(1)


def _rewrite_outbox(remaining: list[dict]) -> None:
    """Atomically rewrite outbox.jsonl with the not-yet-submitted envelopes."""
    tmp = OUTBOX.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for env in remaining:
            fh.write(json.dumps(env, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(OUTBOX)


def drain_once(client: Any) -> tuple[int, int]:
    """Submit every pending envelope; returns (submitted, failed)."""
    if not OUTBOX.exists():
        return 0, 0
    submitted = failed = 0
    remaining: list[dict] = []
    for line in OUTBOX.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            env = json.loads(line)
        except json.JSONDecodeError:
            LOG.warning("dropping corrupt outbox line")
            failed += 1
            continue
        tries = int(env.get("_tries", 0))
        try:
            client.submit_simple_task(
                env["capability"],
                env["payload"],
                name=env.get("name") or "",
                priority=env.get("priority", 5),
            )
            submitted += 1
        except Exception as exc:  # noqa: BLE001
            tries += 1
            env["_tries"] = tries
            remaining.append(env)
            if tries >= MAX_TRIES:
                failed += 1
                _notify(
                    f"Task submit final failed after {tries} attempts: "
                    f"{env.get('capability')} ({exc})"
                )
            else:
                LOG.debug("submit retry %d/%d: %s", tries, MAX_TRIES, exc)
    _rewrite_outbox(remaining)
    return submitted, failed


def drain_loop(client: Any) -> None:
    while True:
        try:
            drain_once(client)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("drain pass error: %s", exc)
        time.sleep(DRAIN_INTERVAL)


def _make_client() -> Any:
    from nodes.common import relay_client as rc
    from nodes.common.node_utils import load_config, load_meta

    return rc.RelayClient(load_meta(), load_config())


def _register(capabilities: list[dict]) -> None:
    """First-boot registration via the app's own /data (secret channel).

    Writes ~/.relay/iowap-agent.json (node_id, registration_secret) and the
    temporary token. The node stays pending until admin approval; the daemon
    may heartbeat-401 meanwhile — it backs off gracefully. The runtime token
    is minted on approval-time refresh.

    Retry ~forever: the relay may be offline at HA boot. The outbox keeps
    buffering stdin meanwhile — nothing is lost.
    """
    import httpx

    from nodes.common.node_utils import save_meta, save_token

    base_url = _load_config()["base_url"]
    body = {"node_name": NODE_NAME,
            "endpoint": None,
            "capabilities": capabilities,
            "role": "worker"}
    while True:
        try:
            r = httpx.post(f"{base_url}/relay/v2/auth/register",
                           json=body, timeout=10)
            r.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001
            LOG.warning("registration failed (retrying in 30s): %s", exc)
            time.sleep(30)

    data = r.json()
    save_meta({"node_id": data["node_id"],
               "node_name": NODE_NAME,
               "registration_secret": data["registration_secret"]})
    save_token(data["token"], expires_at=data.get("expires_at"))
    LOG.warning(
        "registered as node %s — PENDING admin approval on the relay",
        data["node_id"],
    )


def _sigterm(_signum: int, _frame: Any) -> None:
    """Supervisor stops PID 1 (this process) on app stop — exit promptly.

    The node-daemon runs as this container's background child; container
    teardown kills it. The outbox is crash-safe (fsync per line), so a
    mid-drain kill loses nothing.
    """
    LOG.info("SIGTERM — shutting down stdin-listener")
    sys.exit(0)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    cfg = _load_config()
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    # First boot: register before draining. The daemon run.sh started may
    # heartbeat-401 until the meta file exists — acceptable, it backs off.
    if not (DATA / ".relay" / "iowap-agent.json").exists():
        LOG.info("no node registration found — registering with relay")
        _register(_load_caps())

    # stdin reader thread: submit_task envelopes go to the outbox,
    # set_domain_states is applied locally (persist + profile regen + HUP)
    def read_stdin() -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError as exc:
                LOG.warning("dropping non-JSON stdin line: %s", exc)
                continue
            err = _validate(envelope)
            if err:
                LOG.warning("dropping invalid envelope: %s", err)
                continue
            if envelope.get("kind") == "set_domain_states":
                try:
                    applied = _apply_domain_states(envelope["states"])
                    LOG.info("domain states applied: %s", applied)
                    _notify("IOWAP domain modes updated: " + ", ".join(
                        f"{d}={v}" for d, v in sorted(applied.items())))
                except Exception as exc:  # noqa: BLE001
                    LOG.error("set_domain_states failed: %s", exc)
                continue
            _append_outbox(envelope)
            LOG.info("envelope queued: %s", envelope.get("capability"))

    threading.Thread(target=read_stdin, name="stdin-reader", daemon=True).start()

    # T-172b: periodic telemetry push into HA (ready + metrics entities)
    threading.Thread(target=_telemetry_loop, name="telemetry", daemon=True).start()

    client = _make_client()
    LOG.info("stdin-listener up (outbox=%s)", OUTBOX)
    drain_loop(client)


if __name__ == "__main__":
    main()