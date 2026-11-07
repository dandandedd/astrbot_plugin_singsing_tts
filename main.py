"""Tool-driven speech and singing replies for AstrBot on aiocqhttp."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

_DEFAULT_SINGING_PREFIX = "[singsing]"
_DEFAULT_TTS_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_TEXT_CHARS = 1000
_DEFAULT_OUTER_TOOL_TIMEOUT_SECONDS = 120.0
_OUTER_TOOL_TIMEOUT_RESERVE_SECONDS = 2.0
_MIN_EFFECTIVE_TTS_TIMEOUT_SECONDS = 0.1
_MIN_TTS_TIMEOUT_SECONDS = 1.0
_MAX_TTS_TIMEOUT_SECONDS = 300.0
_MIN_TEXT_CHARS = 1
_MAX_TEXT_CHARS = 10000
_VOICE_OUTPUT_CLAIMED_EXTRA = "astrbot_plugin_singsing_tts.voice_output_claimed"


def _coerce_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Normalize and clamp a finite floating-point configuration value.

    Args:
        value: Raw configuration value.
        default: Value used when conversion fails.
        minimum: Inclusive lower bound.
        maximum: Inclusive upper bound.

    Returns:
        The normalized and clamped floating-point value.
    """
    if isinstance(value, bool):
        return default
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(normalized):
        return default
    return min(max(normalized, minimum), maximum)


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Normalize and clamp an integer configuration value.

    Args:
        value: Raw configuration value.
        default: Value used when conversion fails.
        minimum: Inclusive lower bound.
        maximum: Inclusive upper bound.

    Returns:
        The normalized and clamped integer value.
    """
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(normalized, minimum), maximum)


def _coerce_outer_timeout(value: Any) -> float:
    """Normalize the Core local-runner tool timeout.

    Args:
        value: Raw ``agent_runner.config.misc.tool_call_timeout`` value.

    Returns:
        A positive finite timeout, or the AstrBot default of 120 seconds.
    """
    if isinstance(value, bool):
        return _DEFAULT_OUTER_TOOL_TIMEOUT_SECONDS
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_OUTER_TOOL_TIMEOUT_SECONDS
    if not math.isfinite(normalized) or normalized <= 0:
        return _DEFAULT_OUTER_TOOL_TIMEOUT_SECONDS
    return normalized


class SingSingVoicePlugin(Star):
    """Expose one LLM tool for deterministic speech and singing replies."""

    def __init__(
        self,
        context: Context,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize plugin configuration.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration supplied by AstrBot.
        """
        super().__init__(context)
        raw_config: Mapping[str, Any] = config if isinstance(config, Mapping) else {}

        prefix = raw_config.get("singing_prefix", _DEFAULT_SINGING_PREFIX)
        if isinstance(prefix, str) and prefix.strip():
            self.singing_prefix = prefix.strip()
        else:
            self.singing_prefix = _DEFAULT_SINGING_PREFIX
            self.logger.warning(
                "Invalid singing_prefix configuration; using the default."
            )

        self.tts_timeout_seconds = _coerce_float(
            raw_config.get(
                "tts_timeout_seconds",
                _DEFAULT_TTS_TIMEOUT_SECONDS,
            ),
            _DEFAULT_TTS_TIMEOUT_SECONDS,
            _MIN_TTS_TIMEOUT_SECONDS,
            _MAX_TTS_TIMEOUT_SECONDS,
        )
        self.max_text_chars = _coerce_int(
            raw_config.get("max_text_chars", _DEFAULT_MAX_TEXT_CHARS),
            _DEFAULT_MAX_TEXT_CHARS,
            _MIN_TEXT_CHARS,
            _MAX_TEXT_CHARS,
        )

    @filter.llm_tool(name="send_voice")
    async def send_voice(
        self,
        event: AstrMessageEvent,
        text: str = "",
        style: str = "speech",
    ) -> None:
        """主动给用户发送语音。

        当你想用语音回复、用户要求你说话或发语音、或者用户要求你唱歌时调用。
        调用本工具时不要在普通正文中重复语音内容。

        Args:
            text(string): 要朗读或演唱的正文，不要带控制标记。
            style(string): speech 表示普通说话，sing 表示唱歌。
        """
        event.clear_result()
        if text is None or text == "":
            return None
        if not isinstance(text, str):
            self.logger.warning(
                "send_voice text must be a string; ignoring the request."
            )
            return None

        clean_text = self._clean_text(text)
        if not clean_text:
            return None

        if event.get_extra(_VOICE_OUTPUT_CLAIMED_EXTRA, False):
            self.logger.info(
                "send_voice already completed for this event; ignoring duplicate."
            )
            return None
        event.set_extra(_VOICE_OUTPUT_CLAIMED_EXTRA, True)

        normalized_style = self._normalize_style(style)
        if event.get_platform_name() != "aiocqhttp":
            await self._send_plain(event, clean_text)
            return None

        if len(clean_text) > self.max_text_chars:
            self.logger.warning(
                "Voice text exceeds max_text_chars; sending plain text."
            )
            await self._send_plain(event, clean_text)
            return None

        effective_timeout: float | None = None
        try:
            outer_timeout = self._get_outer_tool_call_timeout(event)
            if outer_timeout <= _OUTER_TOOL_TIMEOUT_RESERVE_SECONDS:
                self.logger.warning(
                    "Core tool_call_timeout leaves no safe TTS window; "
                    "sending plain text."
                )
                await self._send_plain(event, clean_text)
                return None

            effective_timeout = min(
                self.tts_timeout_seconds,
                max(
                    _MIN_EFFECTIVE_TTS_TIMEOUT_SECONDS,
                    outer_timeout - _OUTER_TOOL_TIMEOUT_RESERVE_SECONDS,
                ),
            )
            if effective_timeout < self.tts_timeout_seconds:
                self.logger.info(
                    "Capping TTS timeout from %.1f to %.1f seconds to leave "
                    "time for the Core tool timeout and Plain fallback.",
                    self.tts_timeout_seconds,
                    effective_timeout,
                )

            if not await self._tts_enabled(event):
                self.logger.info(
                    "TTS is disabled globally or for this session; sending text."
                )
                await self._send_plain(event, clean_text)
                return None

            provider = await self.context.get_using_tts_provider_async(
                event.unified_msg_origin
            )
            if provider is None:
                self.logger.warning("No TTS provider is available; sending text.")
                await self._send_plain(event, clean_text)
                return None

            tts_text = (
                clean_text
                if normalized_style == "speech"
                else f"{self.singing_prefix}{clean_text}"
            )
            raw_audio_path = await asyncio.wait_for(
                provider.get_audio(tts_text),
                timeout=effective_timeout,
            )
            audio_path = self._resolve_audio_path(raw_audio_path)
            if audio_path is None:
                self.logger.warning(
                    "TTS provider returned an invalid audio path; sending text."
                )
                await self._send_plain(event, clean_text)
                return None

            await event.send(
                MessageChain(
                    [
                        Record(
                            file=audio_path,
                            url=audio_path,
                            text=clean_text,
                        )
                    ]
                )
            )
            return None
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.logger.warning(
                "TTS generation timed out after %.1f seconds; sending text.",
                effective_timeout or self.tts_timeout_seconds,
            )
            await self._send_plain(event, clean_text)
            return None
        except Exception:
            self.logger.exception("Failed to send a voice reply; sending text.")
            await self._send_plain(event, clean_text)
            return None

    def _normalize_style(self, style: Any) -> str:
        """Normalize the requested output style.

        Args:
            style: Raw style value supplied by the LLM.

        Returns:
            Either ``speech`` or ``sing``.
        """
        normalized = str(style).strip().lower() if style is not None else ""
        if normalized in {"speech", "sing"}:
            return normalized
        self.logger.warning("Unsupported voice style; falling back to speech.")
        return "speech"

    def _clean_text(self, text: str) -> str:
        """Remove all internal control markers from user-visible text.

        Args:
            text: Raw string supplied by the LLM.

        Returns:
            Stripped text without the configured or fixed singing marker.
        """
        cleaned = text.strip()
        markers = sorted(
            {self.singing_prefix, _DEFAULT_SINGING_PREFIX},
            key=len,
            reverse=True,
        )
        while True:
            previous = cleaned
            for marker in markers:
                cleaned = cleaned.replace(marker, "")
            cleaned = cleaned.strip()
            if cleaned == previous:
                return cleaned

    async def _tts_enabled(self, event: AstrMessageEvent) -> bool:
        """Check both global and session-level TTS gates.

        Args:
            event: Current message event.

        Returns:
            Whether TTS may be used for this session.
        """
        config = self.context.get_config(event.unified_msg_origin)
        settings = config.get("provider_tts_settings", {})
        if not isinstance(settings, Mapping) or not bool(settings.get("enable")):
            return False
        return await SessionServiceManager.should_process_tts_request(event)

    def _get_outer_tool_call_timeout(self, event: AstrMessageEvent) -> float:
        """Read the Core local-runner tool timeout for the current session.

        Args:
            event: Current message event.

        Returns:
            The configured Core tool-call timeout, or 120 seconds when missing
            or invalid.
        """
        config = self.context.get_config(event.unified_msg_origin)
        agent_runner = (
            config.get("agent_runner", {}) if isinstance(config, Mapping) else {}
        )
        runner_config = (
            agent_runner.get("config", {}) if isinstance(agent_runner, Mapping) else {}
        )
        misc = (
            runner_config.get("misc", {}) if isinstance(runner_config, Mapping) else {}
        )
        raw_timeout = (
            misc.get("tool_call_timeout", _DEFAULT_OUTER_TOOL_TIMEOUT_SECONDS)
            if isinstance(misc, Mapping)
            else _DEFAULT_OUTER_TOOL_TIMEOUT_SECONDS
        )
        return _coerce_outer_timeout(raw_timeout)

    def _resolve_audio_path(self, raw_path: Any) -> str | None:
        """Resolve and validate a provider audio path.

        Args:
            raw_path: Value returned by the TTS provider.

        Returns:
            An absolute local file path, or ``None`` when invalid.
        """
        if raw_path is None:
            return None
        value = str(raw_path).strip()
        if not value:
            return None

        try:
            if is_file_uri(value):
                local_path = file_uri_to_path(value)
            else:
                scheme = urlsplit(value).scheme
                if scheme and len(scheme) != 1:
                    return None
                local_path = value

            path = Path(local_path).expanduser()
            if not path.is_file() or path.stat().st_size <= 0:
                return None
            return str(path.resolve())
        except (OSError, ValueError):
            return None

    async def _send_plain(self, event: AstrMessageEvent, text: str) -> None:
        """Send a plain-text fallback without leaking control markers.

        Args:
            event: Current message event.
            text: Clean text to send.
        """
        if not text:
            return
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception:
            self.logger.exception("Failed to send plain-text fallback.")
