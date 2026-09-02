"""T-180/3 + T-181: dynamic blueprint generation from capability schemas.

For every capability the cluster discovery endpoint reports, this module
generates TWO HA blueprints:

* an automation blueprint under ``<config>/blueprints/automation/iowap/``
  (T-180) — a complete automation whose action runs
  ``iowap.submit_task``. Since T-181 the blueprint also carries optional
  ``trigger`` and ``condition`` inputs (HA 2023.12+ selectors), so the
  whole automation — trigger, condition, action — can be configured from
  the blueprint form.
* a script blueprint under ``<config>/blueprints/script/iowap/``
  (T-181/A) — deriving a script from it yields a regular script entity
  that can be used as an action in ANY automation: trigger → condition →
  run the IOWAP script. One derived script, unlimited automations.

Regeneration is driven by state changes of the server-metrics entity: the
capability list (incl. input_schema) rides on its ``capabilities``
attribute (pushed by the app's telemetry loop, T-179b + T-180 schema).
A per-domain manifest tracks generated files so capabilities that vanish
from the cluster get their blueprints removed. Blueprints are NOT removed
on integration unload — automations/scripts may still reference them.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

BLUEPRINT_DIRNAME = "iowap"
MANIFEST = "manifest.json"
ENTITY_CANDIDATES = (
    "sensor.iowap_server_metrics",  # mirror (registry entity)
    "sensor.iowap_raw_server_metrics",  # raw (app push)
)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return slug or "capability"


def _iter_fields(input_schema: Any) -> list[dict]:
    """Normalize input_schema into a list of field dicts.

    Mirrors the server's CapabilityInputSchema.from_dict: ``fields`` may be
    a dict {name: spec} or a list of specs (each carrying a "name").
    """
    if not isinstance(input_schema, dict) or not input_schema:
        return []
    raw = input_schema.get("fields", input_schema)
    fields: list[dict] = []
    if isinstance(raw, dict):
        for key, spec in raw.items():
            if isinstance(spec, dict):
                fields.append({**spec, "name": str(spec.get("name") or key)})
    elif isinstance(raw, list):
        for i, spec in enumerate(raw):
            if isinstance(spec, dict):
                fields.append({**spec, "name": str(spec.get("name") or f"field_{i}")})
    return [f for f in fields if f.get("name")]


def _field_selector(field: dict) -> dict:
    """Map one schema field to an HA selector (best effort)."""
    ftype = str(field.get("type") or "string").lower()
    is_number = ftype in ("int", "integer", "number", "float")
    # number+enum wins over the bare enum branch: a numeric enum must stay a
    # number selector (enum values would arrive as strings and fail coercion).
    if is_number and field.get("enum") is not None and isinstance(field["enum"], list):
        num: dict[str, Any] = {}
        if field.get("ge") is not None:
            num["min"] = field["ge"]
        if field.get("le") is not None:
            num["max"] = field["le"]
        return {"number": num or None}
    if field.get("enum") is not None and isinstance(field["enum"], list):
        return {"select": {"options": [str(v) for v in field["enum"]]}}
    if ftype in ("bool", "boolean"):
        return {"boolean": None}
    if is_number:
        num = {}
        if field.get("ge") is not None:
            num["min"] = field["ge"]
        if field.get("le") is not None:
            num["max"] = field["le"]
        return {"number": num or None}
    if ftype in ("object", "dict", "map", "list", "array"):
        return {"object": None}
    return {"text": None}


def _yaml_scalar(value: Any) -> str:
    """JSON encoding — a valid YAML subset — for literal scalars."""
    return json.dumps(value)


def _input_block(
    lines: list[str],
    key: str,
    name: str,
    selector: dict[str, Any],
    default: Any = None,
    description: str | None = None,
) -> None:
    """Append one blueprint input definition to ``lines``."""
    lines.append(f"    {key}:")
    lines.append(f"      name: {_yaml_scalar(name)}")
    if description:
        lines.append(f"      description: {_yaml_scalar(description)}")
    lines.append("      selector:")
    sel_key, sel_val = next(iter(selector.items()))
    if isinstance(sel_val, dict):
        if sel_val:
            lines.append(f"        {sel_key}:")
            for k, v in sel_val.items():
                lines.append(f"            {k}: {_yaml_scalar(v)}")
        else:
            lines.append(f"        {sel_key}: null")
    else:
        lines.append(f"        {sel_key}: null")
    if default is not None:
        lines.append(f"      default: {_yaml_scalar(default)}")


def _emit_inputs(lines: list[str], fields: list[dict]) -> None:
    """Emit the shared blueprint inputs (task_name, priority, schema fields)."""
    lines.append("  input:")
    _input_block(lines, "task_name", "Task name (optional)", {"text": None}, "")
    _input_block(
        lines, "priority", "Priority (1 = soon, 9 = late)",
        {"number": {"min": 1, "max": 9}}, 5,
    )
    for f in fields:
        key = f"fld_{_slugify(f['name'])}"
        desc = str(f["description"]) if f.get("description") else (
            None if f.get("required") else "optional")
        default = f.get("default") if not f.get("required") else None
        _input_block(
            lines, key, str(f["name"]), _field_selector(f),
            default=default, description=desc,
        )


def _emit_action(lines: list[str], name: str, fields: list[dict]) -> None:
    """Emit the iowap.submit_task action wired to the inputs."""
    lines.append("  - service: iowap.submit_task")
    lines.append("    data:")
    lines.append(f"      capability: {_yaml_scalar(name)}")
    lines.append("      name: !input task_name")
    lines.append("      priority: !input priority")
    lines.append("      payload:")
    if fields:
        for f in fields:
            lines.append(f"        {f['name']}: !input fld_{_slugify(f['name'])}")
    else:
        lines.append("        {}")


def _build_automation_yaml(cap: dict) -> str | None:
    """Automation blueprint (T-180 + T-181/B: optional trigger/condition)."""
    name = cap.get("name")
    if not isinstance(name, str) or not name:
        return None
    fields = _iter_fields(cap.get("input_schema"))
    desc = str(cap.get("description") or "")

    lines: list[str] = []
    lines.append("blueprint:")
    lines.append(f"  name: {_yaml_scalar(f'IOWAP: {name}')}")
    full_desc = (
        f"Run the IOWAP capability {name} via iowap.submit_task. "
        + (f"{desc} " if desc else "")
        + "Trigger and condition are optional inputs — leave them empty "
        "and wire trigger/condition in the automation editor instead, or "
        "derive a reusable script from the IOWAP script blueprint."
    )
    lines.append(f"  description: {_yaml_scalar(full_desc)}")
    lines.append("  domain: automation")
    lines.append("  author: IOWAP")
    _emit_inputs(lines, fields)
    # T-181/B: optional trigger/condition inputs. Empty defaults keep the
    # blueprint usable exactly like the T-180 versions (edit trigger in UI).
    _input_block(
        lines, "triggers", "Triggers (optional)",
        {"trigger": None}, [],
        description="Automation triggers; leave empty to add them in the editor.",
    )
    _input_block(
        lines, "conditions", "Conditions (optional)",
        {"condition": None}, [],
        description="Conditions that must pass before the task is submitted.",
    )
    lines.append("trigger: !input triggers")
    lines.append("condition: !input conditions")
    lines.append("variables:")
    lines.append("  __payload: {}")
    lines.append("action:")
    _emit_action(lines, name, fields)
    lines.append("mode: single")
    lines.append("max_exceeded: silent")
    return "\n".join(lines) + "\n"


def _build_script_yaml(cap: dict) -> str | None:
    """Script blueprint (T-181/A): reusable submit_task action as a script."""
    name = cap.get("name")
    if not isinstance(name, str) or not name:
        return None
    fields = _iter_fields(cap.get("input_schema"))
    desc = str(cap.get("description") or "")

    lines: list[str] = []
    lines.append("blueprint:")
    lines.append(f"  name: {_yaml_scalar(f'IOWAP: {name}')}")
    full_desc = (
        f"Run the IOWAP capability {name} via iowap.submit_task. "
        + (f"{desc} " if desc else "")
        + "Derive a script from this blueprint once and use it as an "
        "action in any automation (trigger/condition live in that "
        "automation, not here)."
    )
    lines.append(f"  description: {_yaml_scalar(full_desc)}")
    lines.append("  domain: script")
    lines.append("  author: IOWAP")
    _emit_inputs(lines, fields)
    lines.append("sequence:")
    _emit_action(lines, name, fields)
    return "\n".join(lines) + "\n"


_BUILDERS = {
    "automation": _build_automation_yaml,
    "script": _build_script_yaml,
}


def _read_entity_caps(hass: Any) -> list[dict]:
    """Capabilities (incl. input_schema) from the server-metrics entity."""
    for eid in ENTITY_CANDIDATES:
        st = hass.states.get(eid)
        caps = st.attributes.get("capabilities") if st else None
        if isinstance(caps, list) and caps:
            return [c for c in caps if isinstance(c, dict) and c.get("name")]
    return []


def regenerate(hass: Any, caps: list[dict] | None = None) -> dict:
    """(Re)write automation + script blueprints; delete vanished ones.

    Returns stats {written, deleted, total} summed over both blueprint
    domains. Safe to call with an empty capability list (e.g. relay
    down): then nothing is deleted — stale blueprints keep working until
    the cluster says otherwise.
    """
    if caps is None:
        caps = _read_entity_caps(hass)
    written = 0
    deleted = 0
    total = 0
    for domain, builder in _BUILDERS.items():
        bp_dir = Path(hass.config.path(f"blueprints/{domain}")) / BLUEPRINT_DIRNAME
        wanted: dict[str, str] = {}
        for cap in caps:
            fname = f"iowap_cap_{_slugify(str(cap['name']))}.yaml"
            yaml_text = builder(cap)
            if yaml_text:
                wanted[fname] = yaml_text

        if not wanted:
            continue
        total += len(wanted)

        manifest_path = bp_dir / MANIFEST
        previous: set[str] = set()
        try:
            previous = set(json.loads(manifest_path.read_text())["generated"])
        except Exception:  # noqa: BLE001 (first run / corrupt manifest)
            pass

        bp_dir.mkdir(parents=True, exist_ok=True)
        for fname, yaml_text in wanted.items():
            path = bp_dir / fname
            try:
                if not path.exists() or path.read_text() != yaml_text:
                    path.write_text(yaml_text, encoding="utf-8")
                    written += 1
            except OSError as exc:
                _LOGGER.warning("blueprint write failed (%s): %s", fname, exc)

        for fname in sorted(previous - set(wanted)):
            try:
                (bp_dir / fname).unlink()
                deleted += 1
            except OSError as exc:
                _LOGGER.debug("blueprint delete failed (%s): %s", fname, exc)

        try:
            manifest_path.write_text(
                json.dumps({"generated": sorted(wanted)}), encoding="utf-8"
            )
        except OSError as exc:
            _LOGGER.warning("blueprint manifest write failed: %s", exc)

    _LOGGER.info(
        "blueprints refreshed: %d per domain, %d written, %d removed",
        total, written, deleted,
    )
    return {"written": written, "deleted": deleted, "total": total}