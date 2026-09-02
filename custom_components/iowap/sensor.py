"""T-178: sensor platform — mirrors raw pushed IOWAP entities.

Sources are state-machine-only entities (``sensor.iowap_raw_*``) written by
the app container via the Supervisor proxy. Mirrors add unique_id + device
registry presence and ``unavailable`` semantics when the raw state is
missing or stale.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
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
            NodeMetricsMirror(hass, entry.entry_id),
            ServerMetricsMirror(hass, entry.entry_id),
            TasksMirror(hass, entry.entry_id),
        ]
    )


class _SensorMirrorBase(RawMirrorEntity, SensorEntity):
    """Shared plumbing for sensor mirrors."""

    def _raw(self) -> Any:
        return None

    @property
    def native_value(self):
        raise NotImplementedError


class NodeMetricsMirror(_SensorMirrorBase):
    """sensor.iowap_node_metrics — mirrors sensor.iowap_raw_node_metrics."""

    _attr_unique_id = f"{DOMAIN}-node-metrics"
    _attr_name = "IOWAP Node Metrics"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _raw_entity_id = "sensor.iowap_raw_node_metrics"
    _attr_icon = "mdi:chart-box"

    @property
    def native_value(self):
        if self._raw_state is None or self._raw_state.state in ("unavailable", "unknown"):
            return None
        return self._raw_state.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._mirror_attributes()

    @property
    def native_unit_of_measurement(self):
        return self._raw_attrs.get("unit_of_measurement", "capabilities")


class ServerMetricsMirror(_SensorMirrorBase):
    """sensor.iowap_server_metrics — mirrors sensor.iowap_raw_server_metrics.

    State = nodes online; carries the cluster capabilities attribute
    (T-179b) pushed by the app's telemetry loop.
    """

    _attr_unique_id = f"{DOMAIN}-server-metrics"
    _attr_name = "IOWAP Server Metrics"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _raw_entity_id = "sensor.iowap_raw_server_metrics"
    _attr_icon = "mdi:server-network"

    @property
    def native_value(self):
        if self._raw_state is None or self._raw_state.state in ("unavailable", "unknown"):
            return None
        return self._raw_state.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._mirror_attributes()

    @property
    def native_unit_of_measurement(self):
        return self._raw_attrs.get("unit_of_measurement", "nodes online")


class TasksMirror(_SensorMirrorBase):
    """sensor.iowap_tasks — mirrors sensor.iowap_raw_tasks (T-179a).

    State = number of tasks in flight; attributes carry last task id/status
    and per-task statuses.
    """

    _attr_unique_id = f"{DOMAIN}-tasks"
    _attr_name = "IOWAP Tasks"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _raw_entity_id = "sensor.iowap_raw_tasks"
    _attr_icon = "mdi:format-list-checks"

    @property
    def native_value(self):
        if self._raw_state is None or self._raw_state.state in ("unavailable", "unknown"):
            return None
        return self._raw_state.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._mirror_attributes()

    @property
    def native_unit_of_measurement(self):
        return self._raw_attrs.get("unit_of_measurement", "in flight")