"""T-178: binary_sensor platform — mirrors raw pushed IOWAP entities.

Sources are state-machine-only entities (``binary_sensor.iowap_raw_*``)
written by the app container via the Supervisor proxy. Mirrors add
unique_id + device registry presence and ``unavailable`` semantics when
the raw state is missing or stale.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .mirror import RawMirrorEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities(
        [
            NodeReadyMirror(entry.entry_id),
            ServerReadyMirror(entry.entry_id),
            LastTaskMirror(entry.entry_id),
        ]
    )


class _BinaryMirrorBase(RawMirrorEntity, BinarySensorEntity):
    """Shared plumbing for binary_sensor mirrors."""


class NodeReadyMirror(_BinaryMirrorBase):
    """binary_sensor.iowap_node_ready — mirrors binary_sensor.iowap_raw_node_ready."""

    _attr_unique_id = f"{DOMAIN}-node-ready"
    _attr_name = "IOWAP Node Ready"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _raw_entity_id = "binary_sensor.iowap_raw_node_ready"
    _attr_icon = "mdi:cloud-check"

    @property
    def is_on(self) -> bool | None:
        if self._raw_state is None:
            return None
        return self._raw_state.state == "on"


class ServerReadyMirror(_BinaryMirrorBase):
    """binary_sensor.iowap_server_ready — mirrors binary_sensor.iowap_raw_server_ready."""

    _attr_unique_id = f"{DOMAIN}-server-ready"
    _attr_name = "IOWAP Server Ready"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _raw_entity_id = "binary_sensor.iowap_raw_server_ready"
    _attr_icon = "mdi:server"

    @property
    def is_on(self) -> bool | None:
        if self._raw_state is None:
            return None
        return self._raw_state.state == "on"


class LastTaskMirror(_BinaryMirrorBase):
    """binary_sensor.iowap_last_task — mirrors binary_sensor.iowap_raw_last_task.

    T-182: the app's last finished task. State is ``on`` once a result has
    been pushed (constant — a state trigger on attribute change fires when
    a new result arrives); attributes carry the verbatim result dict under
    ``last_result`` plus ``result_truncated``, ``last_result_task_id``,
    ``last_result_status`` and ``last_result_updated_iso``. No device
    class: this is a semantic "fresh result available" flag.
    """

    _attr_unique_id = f"{DOMAIN}-last-task"
    _attr_name = "IOWAP Last Task"
    _raw_entity_id = "binary_sensor.iowap_raw_last_task"
    _attr_icon = "mdi:bell-ring"

    @property
    def is_on(self) -> bool | None:
        if self._raw_state is None:
            return None
        return self._raw_state.state == "on"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._mirror_attributes()