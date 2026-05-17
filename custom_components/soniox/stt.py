"""Soniox real-time speech-to-text platform."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterable

import aiohttp

from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SonioxConfigEntry
from .const import (
    CONF_API_KEY,
    CONF_STT_MODEL,
    DEFAULT_STT_MODEL,
    DOMAIN,
    STT_WEBSOCKET_URL,
    SUPPORTED_LANGUAGES,
)

_LOGGER = logging.getLogger(__name__)

# Soniox's preferred raw encoding for low-latency streaming.
_AUDIO_FORMAT = "pcm_s16le"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonioxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register the Soniox STT entity for this config entry."""
    async_add_entities([SonioxSTTEntity(entry)])


class SonioxSTTEntity(SpeechToTextEntity):
    """Streams Home Assistant audio to Soniox and returns the final transcript."""

    _attr_has_entity_name = True
    _attr_name = "Speech-to-Text"

    def __init__(self, entry: SonioxConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-stt"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Soniox",
            manufacturer="Soniox",
            model="Speech AI",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str]:
        return SUPPORTED_LANGUAGES

    @property
    def supported_formats(self) -> list[AudioFormats]:
        return [AudioFormats.WAV, AudioFormats.OGG]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [
            AudioSampleRates.SAMPLERATE_8000,
            AudioSampleRates.SAMPLERATE_16000,
            AudioSampleRates.SAMPLERATE_44100,
            AudioSampleRates.SAMPLERATE_48000,
        ]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Open a Soniox realtime session, push audio, collect the transcript."""
        api_key: str = self._entry.data[CONF_API_KEY]
        model: str = self._entry.options.get(CONF_STT_MODEL, DEFAULT_STT_MODEL)
        language = (metadata.language or "en").split("-", 1)[0].lower()

        config_msg = {
            "api_key": api_key,
            "model": model,
            "audio_format": _AUDIO_FORMAT,
            "sample_rate": int(metadata.sample_rate),
            "num_channels": int(metadata.channel),
            "language_hints": [language],
            "enable_endpoint_detection": True,
        }

        session = async_get_clientsession(self.hass)
        try:
            async with session.ws_connect(
                STT_WEBSOCKET_URL,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=30),
                max_msg_size=0,
            ) as ws:
                await ws.send_json(config_msg)
                text = await self._run_session(ws, stream)
        except aiohttp.ClientError as err:
            _LOGGER.error("Soniox STT connection failed: %s", err)
            return SpeechResult(None, SpeechResultState.ERROR)
        except TimeoutError:
            _LOGGER.error("Soniox STT timed out")
            return SpeechResult(None, SpeechResultState.ERROR)

        if not text:
            return SpeechResult("", SpeechResultState.ERROR)
        return SpeechResult(text, SpeechResultState.SUCCESS)

    async def _run_session(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        stream: AsyncIterable[bytes],
    ) -> str | None:
        """Pump audio in and collect final tokens out, concurrently."""
        final_tokens: list[str] = []
        send_error: BaseException | None = None

        async def send_audio() -> None:
            nonlocal send_error
            try:
                async for chunk in stream:
                    if not chunk:
                        continue
                    await ws.send_bytes(chunk)
                # Empty string signals end-of-audio to Soniox.
                await ws.send_str("")
            except Exception as err:  # noqa: BLE001 — surface in receive loop
                send_error = err

        send_task = asyncio.create_task(send_audio())

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    if err_code := payload.get("error_code"):
                        _LOGGER.error(
                            "Soniox STT error %s: %s",
                            err_code,
                            payload.get("error_message"),
                        )
                        return None
                    for token in payload.get("tokens", []):
                        if token.get("is_final") and (text := token.get("text")):
                            final_tokens.append(text)
                    if payload.get("finished"):
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if send_error is not None:
            _LOGGER.error("Soniox STT audio upload failed: %s", send_error)
            return None

        return "".join(final_tokens).strip() or None
