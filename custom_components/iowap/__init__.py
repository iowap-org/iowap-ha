"""IOWAP integration for Home Assistant.

Thin glue layer: the IOWAP app container (same repo, `iowap/` directory)
holds the relay node daemon and the only relay token. HA core never talks
to the relay directly. This integration provides:

- Config entry (capability scope options land with the options flow, v2)
- `iowap.submit_task` service: forwards tasks to the app via the official
  `hassio.app_stdin` service (fields: app, input). The app keeps an outbox,
  so submissions survive app restarts; results flow back as HA states in v2.
"""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.device_registry import async_get

from .const import APP_SLUG, DOMAIN

_LOGGER = logging.getLogger(__name__)

SUBMIT_SCHEMA = vol.Schema(
    {
        vol.Required("capability"): cv.string,
        vol.Required("payload"): dict,
        vol.Optional("name"): cv.string,
        vol.Optional("priority", default=5): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=9)
        ),
    }
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def async_get_options_flow(config_entry):
    """Get the options flow for this handler (per-domain exposure modes)."""
    return OptionsFlow()


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Minimal setup without a config entry (v1 stub)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Set up from a config entry: submit_task service + mirror platforms."""
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "binary_sensor"])
    return await _register_services(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unload = await hass.config_entries.async_unload_platforms(entry, ["sensor", "binary_sensor"])
    hass.services.async_remove(DOMAIN, "submit_task")
    return unload


async def _options_updated(hass: HomeAssistant, entry) -> None:
    """Options flow saved — push the new domain map to the app."""
    from .helpers import push_to_app

    states = entry.options.get("domain_states", {})
    await push_to_app(hass, {"kind": "set_domain_states", "states": states})
    _LOGGER.info("pushed domain states to app: %s", states)


async def _register_services(hass: HomeAssistant, entry) -> bool:
    """Register the submit_task service (slug resolution as in v1)."""

    # Resolve the runtime app slug from the device registry. HAOS prefixes
    # local repo slugs (e.g. `16b71320_iowap`), so the bare repo name is not
    # a valid `app` argument for hassio services. The app device carries the
    # identifier ("hassio", "<slug>") — match it against APP_SLUG as suffix.
    device_registry = async_get(hass)
    app_slug = None
    for device in device_registry.devices.values():
        # Identifiers can be longer than ("domain", "value"): HomeKit bridge
        # devices carry ("homekit", <id>, "homekit.bridge") since HA 2026.x.
        # Index-based matching instead of strict tuple unpacking.
        for ident in device.identifiers:
            if (
                isinstance(ident, (list, tuple))
                and len(ident) >= 2
                and ident[0] == "hassio"
                and str(ident[1]).endswith(APP_SLUG)
            ):
                app_slug = str(ident[1])
                break
        if app_slug:
            break
    if not app_slug:  # safety net: try the bare name (covers supervisor quirk)
        app_slug = APP_SLUG
    _LOGGER.debug("resolved IOWAP app slug: %s", app_slug)

    async def _handle_submit(call: ServiceCall) -> None:
        envelope = {
            "kind": "submit_task",
            "capability": call.data["capability"],
            "payload": call.data["payload"],
            "name": call.data.get("name", ""),
            "priority": call.data.get("priority", 5),
        }
        # hassio.app_stdin: official core->app channel. The service takes
        # {"app": slug, "input": <json-serializable>} and hands the serialized
        # payload to the app container (verified against hassio/services.py).
        await hass.services.async_call(
            "hassio",
            "app_stdin",
            {"app": app_slug, "input": envelope},
            blocking=True,
        )
        _LOGGER.debug(
            "submit_task envelope handed to app %s: %s", app_slug, envelope
        )

    hass.services.async_register(
        DOMAIN, "submit_task", _handle_submit, schema=SUBMIT_SCHEMA
    )
    return True