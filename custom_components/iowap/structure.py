"""T-183b: HA registry snapshot collector + push (kind: set_ha_structure).

The entity/area registries are WebSocket-only in HA core (the legacy
``/api/config/*_registry`` REST endpoints are gone — 404) and the
supervisor core proxy has no substitute. Core-side is native: this
collector serializes the registries and pushes them over the same
``hassio.app_stdin`` channel the domain-states flow uses. The app
persists the snapshot (``/data/ha_structure.json``);
``ha-exec.py``'s ``run_info`` (``ha.info.native``) serves it with the
exposure/scope filters applied — no operational state values ever
travel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.area_registry import EVENT_AREA_REGISTRY_UPDATED
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED

from . import helpers

_LOGGER = logging.getLogger(__name__)

_DEBOUNCE_HANDLE: Any = None
_DEBOUNCE_SECONDS = 5.0


def collect_structure(hass: Any) -> dict:
    """Serialize area + entity registries (registry facts only, no states)."""
    ent_reg = er.async_get(hass)
    area_reg = ar.async_get(hass)
    areas = [{"area_id": a.id, "name": a.name} for a in area_reg.async_list_areas()]
    entities = [
        {
            "entity_id": e.entity_id,
            "area_id": e.area_id,
            "name": e.name,
            "original_name": e.original_name,
            "disabled_by": e.disabled_by,
            "hidden_by": e.hidden_by,
        }
        for e in ent_reg.entities.values()
    ]
    return {
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "areas": areas,
        "entities": entities,
    }


async def push_structure(hass: Any) -> None:
    """Collect + hand the snapshot to the app (official app_stdin channel)."""
    structure = await hass.async_add_executor_job(collect_structure, hass)
    await helpers.push_to_app(
        hass, {"kind": "set_ha_structure", "structure": structure}
    )
    _LOGGER.info(
        "pushed HA structure snapshot: %d areas / %d entities",
        len(structure["areas"]), len(structure["entities"]),
    )


def track_registry_changes(hass: Any, entry: Any) -> None:
    """Initial snapshot at setup + debounced re-push on registry mutation."""

    async def _push(_now: Any = None) -> None:
        await push_structure(hass)

    hass.async_create_task(_push())

    async def _debounced_push(_at: Any) -> None:
        await push_structure(hass)

    def schedule() -> None:
        global _DEBOUNCE_HANDLE
        handle = _DEBOUNCE_HANDLE
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # noqa: BLE001 — a dead timer must not break the update
                pass
        _DEBOUNCE_HANDLE = hass.async_call_later(_DEBOUNCE_SECONDS, _debounced_push)

    def on_registry_event(_event: Any) -> None:
        schedule()

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_ENTITY_REGISTRY_UPDATED, on_registry_event)
    )
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_AREA_REGISTRY_UPDATED, on_registry_event)
    )