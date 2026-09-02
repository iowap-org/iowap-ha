"""T-180/3: dynamic blueprint generation from cluster capability schemas.

For every capability the cluster discovery endpoint reports, this module
generates one HA automation blueprint under
``<config>/blueprints/automation/iowap/``. Selecting the blueprint in the
automation editor gives the user a real form (task name, priority, and one
field per input_schema entry) instead of hand-writing JSON payloads —
the "capability → Hello World → Text Ping" drill-down.

Regeneration is driven by state changes of the server-metrics entity: the
capability list (incl. input_schema) rides on its ``capabilities``
attribute (pushed by the app's telemetry loop, T-179b + T-180 schema).
A manifest tracks generated files so capabilities that vanish from the
cluster get their blueprints removed. Blueprints are NOT removed on
integration unload — automations may still reference them.
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
    if field.get("enum") is not None and isinstance(field["enum"], list):
        return {"select": {"options": [str(v) for v in field["enum"]]}}
    if ftype in ("bool", "boolean"):
        return {"boolean": None}
    if ftype in ("int", "integer", "number", "float"):
        num: dict[str, Any] = {}
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


def _build_blueprint_yaml(cap: dict) -> str | None:
    """One blueprint YAML for one capability. None when not renderable."""
    name = cap.get("name")
    if not isinstance(name, str) or not name:
        return None
    fields = _iter_fields(cap.get("input_schema"))

    lines: list[str] = []
    lines.append("blueprint:")
    lines.append(f"  name: {_yaml_scalar(f'IOWAP: {name}')}")
    desc = str(cap.get("description") or "")
    full_desc = (
        f"Run the IOWAP capability {name} via iowap.submit_task. "
        + (f"{desc} " if desc else "")
        + "Fields below follow the capability's input_schema (cluster discovery)."
    )
    lines.append(f"  description: {_yaml_scalar(full_desc)}")
    lines.append("  domain: automation")
    lines.append("  author: IOWAP")
    lines.append("  input:")
    lines.append("    task_name:")
    lines.append("      name: Task name (optional)")
    lines.append("      default: ''")
    lines.append("      selector:")
    lines.append("        text: null")
    lines.append("    priority:")
    lines.append("      name: Priority (1 = soon, 9 = late)")
    lines.append("      default: 5")
    lines.append("      selector:")
    lines.append("        number:")
    lines.append("          min: 1")
    lines.append("          max: 9")
    for f in fields:
        lines.append(f"    fld_{_slugify(f['name'])}:")
        lines.append(f"      name: {_yaml_scalar(str(f['name']))}")
        if f.get("description"):
            lines.append(f"      description: {_yaml_scalar(str(f['description']))}")
        elif not f.get("required"):
            lines.append("      description: optional")
        sel = _field_selector(f)
        lines.append("      selector:")
        sel_key, sel_val = next(iter(sel.items()))
        if isinstance(sel_val, dict):
            if sel_val:
                lines.append(f"        {sel_key}:")
                for k, v in sel_val.items():
                    lines.append(f"            {k}: {_yaml_scalar(v)}")
            else:
                lines.append(f"        {sel_key}: null")
        else:
            lines.append(f"        {sel_key}: null")
        if f.get("default") is not None and not f.get("required"):
            lines.append(f"      default: {_yaml_scalar(f['default'])}")

    lines.append("trigger: []")
    lines.append("condition: []")
    lines.append("variables:")
    lines.append("  __payload: {}")
    lines.append("action:")
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
    lines.append("mode: single")
    lines.append("max_exceeded: silent")
    return "\n".join(lines) + "\n"


def _read_entity_caps(hass: Any) -> list[dict]:
    """Capabilities (incl. input_schema) from the server-metrics entity."""
    for eid in ENTITY_CANDIDATES:
        st = hass.states.get(eid)
        caps = st.attributes.get("capabilities") if st else None
        if isinstance(caps, list) and caps:
            return [c for c in caps if isinstance(c, dict) and c.get("name")]
    return []


def regenerate(hass: Any, caps: list[dict] | None = None) -> dict:
    """(Re)write blueprints for all capabilities; delete vanished ones.

    Returns stats {written, deleted, total}. Safe to call with an empty
    capability list (e.g. relay down): then nothing is deleted — stale
    blueprints keep working until the cluster says otherwise.
    """
    if caps is None:
        caps = _read_entity_caps(hass)
    bp_dir = Path(hass.config.path("blueprints/automation")) / BLUEPRINT_DIRNAME
    wanted: dict[str, str] = {}
    for cap in caps:
        fname = f"iowap_cap_{_slugify(str(cap['name']))}.yaml"
        yaml_text = _build_blueprint_yaml(cap)
        if yaml_text:
            wanted[fname] = yaml_text

    if not wanted:
        return {"written": 0, "deleted": 0, "total": 0}

    manifest_path = bp_dir / MANIFEST
    previous: set[str] = set()
    try:
        previous = set(json.loads(manifest_path.read_text())["generated"])
    except Exception:  # noqa: BLE001 (first run / corrupt manifest)
        pass

    bp_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for fname, yaml_text in wanted.items():
        path = bp_dir / fname
        try:
            if not path.exists() or path.read_text() != yaml_text:
                path.write_text(yaml_text, encoding="utf-8")
                written += 1
        except OSError as exc:
            _LOGGER.warning("blueprint write failed (%s): %s", fname, exc)

    deleted = 0
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
        "blueprints refreshed: %d capabilities, %d written, %d removed",
        len(wanted), written, deleted,
    )
    return {"written": written, "deleted": deleted, "total": len(wanted)}