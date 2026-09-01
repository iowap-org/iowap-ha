"""Shared helpers for the IOWAP integration."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import async_get

from .const import APP_SLUG

_LOGGER = logging.getLogger(__name__)


def resolve_app_slug(hass: HomeAssistant) -> str:
    """Resolve the runtime app slug from the device registry.

    HAOS prefixes local repo slugs (e.g. `16b71320_iowap`), so the bare
    repo name is not a valid `app` argument for hassio services. The app
    device carries the identifier ("hassio", "<slug>") — match it against
    APP_SLUG as suffix.
    """
    device_registry = async_get(hass)
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
                return str(ident[1])
    return APP_SLUG  # safety net: try the bare name (covers supervisor quirk)


async def push_to_app(hass: HomeAssistant, envelope: dict) -> None:
    """Send an envelope to the app container via hassio.app_stdin.

    Official core->app channel. The service takes {"app": slug,
    "input": <json-serializable>} and hands the serialized payload to the
    app container (verified against hassio/services.py).
    """
    app_slug = resolve_app_slug(hass)
    await hass.services.async_call(
        "hassio",
        "app_stdin",
        {"app": app_slug, "input": envelope},
        blocking=True,
    )
    _LOGGER.debug("envelope handed to app %s: %s", app_slug, envelope)