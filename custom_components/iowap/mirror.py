"""T-178: mirror raw pushed entities into registry-backed HA entities.

The app container pushes telemetry as state-machine-only entities under the
``iowap_raw_*`` namespace (no unique_id, no registry entry — they vanish
without a trace when the app dies). This module maps them onto official
entities: real unique_ids, device registry entry, and proper ``unavailable``
semantics (raw state missing or stale → mirror unavailable).

No new data channel: the mirror reads ``hass.states.get()`` — the raw
entities remain the single source of truth, written by the app's
telemetry loop (stdin_listener.py) via the Supervisor proxy.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.const import ATTR_FRIENDLY_NAME, ATTR_ICON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Attributes never copied from the raw state (the mirror defines its own).
_SKIP_ATTRS = frozenset({ATTR_FRIENDLY_NAME, ATTR_ICON, "restored"})

# Raw entities are pushed every status_push_interval (default 60s, advertised
# on the raw ready entities as "push_interval"). A raw state not updated for
# STALE_FACTOR × interval is stale → the mirror goes unavailable.
STALE_FACTOR = 3
STALE_FLOOR_S = 90
TICK_INTERVAL = timedelta(seconds=30)


class RawMirrorEntity(Entity):
    """Base: mirrors one raw pushed entity, tracks staleness, owns the device."""

    _attr_should_poll = False
    _raw_entity_id: str = ""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._raw_attrs: dict[str, Any] = {}
        self._raw_state: State | None = None
        self._unsub: list[Any] = []

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "name": "IOWAP",
            "manufacturer": "iowap-org",
            "configuration_url": "https://github.com/iowap-org/iowap-ha",
        }

    @property
    def available(self) -> bool:
        state = self._raw_state
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        try:
            age = self.hass.loop.time() - state.last_updated_timestamp
        except AttributeError:
            return True
        return age <= self._stale_seconds()

    def _stale_seconds(self) -> float:
        """Push interval advertised by the app (attr), fallback 60s."""
        try:
            interval = int(self._raw_attrs.get("push_interval") or 60)
        except (TypeError, ValueError):
            interval = 60
        return max(STALE_FLOOR_S, STALE_FACTOR * interval)

    async def async_added_to_hass(self) -> None:
        self._absorb(self.hass.states.get(self._raw_entity_id))
        self._unsub.append(
            async_track_state_change_event(
                self.hass, [self._raw_entity_id], self._on_state_event
            )
        )
        self._unsub.append(
            async_track_time_interval(self.hass, self._on_tick, TICK_INTERVAL)
        )

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _on_state_event(self, event: Event[EventStateChangedData]) -> None:
        self._absorb(event.data.get("new_state"))
        self.async_write_ha_state()

    @callback
    def _on_tick(self, _now: Any) -> None:
        self.async_write_ha_state()

    @callback
    def _absorb(self, state: State | None) -> None:
        self._raw_state = state
        self._raw_attrs = dict(state.attributes) if state else {}

    def _mirror_attributes(self) -> dict[str, Any]:
        return {k: v for k, v in self._raw_attrs.items() if k not in _SKIP_ATTRS}