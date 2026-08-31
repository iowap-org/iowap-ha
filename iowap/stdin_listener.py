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

VALID_KINDS = {"submit_task"}


def _load_config() -> dict[str, Any]:
    cfg_path = DATA / ".relay" / "relay_config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.setdefault("request_timeout", 10)
    return cfg


def _load_caps() -> list[dict[str, Any]]:
    """Capability list from ha-exec.py's CAPS table (single source of truth)."""
    out = subprocess.run(
        ["python3", "/usr/local/bin/ha-exec.py", "--caps-json"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    return json.loads(out)


def _validate(envelope: dict) -> str | None:
    """Return an error string if the envelope is not acceptable."""
    if envelope.get("kind") not in VALID_KINDS:
        return f"unknown kind {envelope.get('kind')!r}"
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

    # stdin reader thread: every valid line goes straight to the outbox
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
            _append_outbox(envelope)
            LOG.info("envelope queued: %s", envelope.get("capability"))

    threading.Thread(target=read_stdin, name="stdin-reader", daemon=True).start()

    client = _make_client()
    LOG.info("stdin-listener up (outbox=%s)", OUTBOX)
    drain_loop(client)


if __name__ == "__main__":
    main()