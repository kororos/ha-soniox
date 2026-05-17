"""The Soniox integration — speech-to-text and text-to-speech via Soniox AI."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.STT, Platform.TTS]

type SonioxConfigEntry = ConfigEntry[None]


async def async_setup_entry(hass: HomeAssistant, entry: SonioxConfigEntry) -> bool:
    """Set up Soniox from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SonioxConfigEntry) -> bool:
    """Unload a Soniox config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: SonioxConfigEntry) -> None:
    """Reload when options change so STT/TTS pick up new defaults."""
    await hass.config_entries.async_reload(entry.entry_id)
