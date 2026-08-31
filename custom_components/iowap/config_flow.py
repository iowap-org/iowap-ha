"""Config flow for the IOWAP integration (v1 stub).

V1 keeps setup minimal: a single confirmation step. Capability/entity-scope
editing (options flow writing /data/options.json via the app) lands with the
capability-config mechanic; see T-169 design doc.
"""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the IOWAP config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm install; real option wiring comes with the options flow."""
        if user_input is not None:
            return self.async_create_entry(title="IOWAP", data={})

        return self.async_show_form(step_id="user")