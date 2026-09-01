"""Options flow for the IOWAP integration: per-domain exposure modes (T-173).

One Select per domain from the app's CAPS matrix — on / readonly / off.
Saving pushes the whole map to the app container via the official
`hassio.app_stdin` channel ({kind: "set_domain_states"}); the app persists
it and republishes the reduced capability set to the relay. Nothing is
stored relay-side: the app is the single source of truth.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_DOMAIN_STATES, DOMAIN_STATE_OPTIONS, DOMAIN_STATES_LIST


class OptionsFlow(config_entries.OptionsFlow):
    """Handle IOWAP options: domain exposure modes."""

    def __init__(self) -> None:
        """Initialize the options flow (config entry set by the handler)."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        if user_input is not None:
            states = {
                d: user_input[d]
                for d in DOMAIN_STATES_LIST
                if user_input.get(d) != "readonly"
            }
            # Options save fires the update listener in __init__, which
            # pushes the map to the app — no second push here.
            return self.async_create_entry(title="", data={CONF_DOMAIN_STATES: states})

        saved = dict(self.config_entry.options.get(CONF_DOMAIN_STATES, {}))
        schema = vol.Schema(
            {
                vol.Required(
                    domain, default=saved.get(domain, "readonly")
                ): vol.In(DOMAIN_STATE_OPTIONS)
                for domain in DOMAIN_STATES_LIST
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "note": (
                    "Read-only is the safe default. 'Off' removes the domain "
                    "from the relay entirely — agents cannot even read its "
                    "state. Changes apply within one heartbeat (~30 s)."
                )
            },
        )