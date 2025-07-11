#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""XTTS text-to-speech service implementation.

This module provides integration with Coqui XTTS streaming server for
text-to-speech synthesis using local Docker deployment.
"""

from typing import Any, AsyncGenerator, Dict, Optional

import aiohttp
from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.tracing.service_decorators import traced_tts

# The server below can connect to XTTS through a local running docker
#
# Docker command: $ docker run --gpus=all -e COQUI_TOS_AGREED=1 --rm -p 8000:80 ghcr.io/coqui-ai/xtts-streaming-server:latest-cuda121
#
# You can find more information on the official repo:
# https://github.com/coqui-ai/xtts-streaming-server


def language_to_xtts_language(language: Language) -> Optional[str]:
    """Convert a Language enum to XTTS language code.

    Args:
        language: The Language enum value to convert.

    Returns:
        The corresponding XTTS language code, or None if not supported.
    """
    BASE_LANGUAGES = {
        Language.CS: "cs",
        Language.DE: "de",
        Language.EN: "en",
        Language.ES: "es",
        Language.FR: "fr",
        Language.HI: "hi",
        Language.HU: "hu",
        Language.IT: "it",
        Language.JA: "ja",
        Language.KO: "ko",
        Language.NL: "nl",
        Language.PL: "pl",
        Language.PT: "pt",
        Language.RU: "ru",
        Language.TR: "tr",
        # Special case for Chinese base language
        Language.ZH: "zh-cn",
    }

    result = BASE_LANGUAGES.get(language)

    # If not found in base languages, try to find the base language from a variant
    if not result:
        # Convert enum value to string and get the base language part (e.g. es-ES -> es)
        lang_str = str(language.value)
        base_code = lang_str.split("-")[0].lower()

        # Special handling for Chinese variants
        if base_code == "zh":
            result = "zh-cn"
        else:
            # Look up the base code in our supported languages
            result = base_code if base_code in BASE_LANGUAGES.values() else None

    return result


class XTTSService(TTSService):
    """Coqui XTTS text-to-speech service.

    Provides text-to-speech synthesis using a locally running Coqui XTTS
    streaming server. Supports multiple languages and voice cloning through
    studio speakers configuration.
    """

    def __init__(
        self,
        *,
        voice_id: str,
        base_url: str,
        aiohttp_session: aiohttp.ClientSession,
        language: Language = Language.EN,
        sample_rate: Optional[int] = None,
        **kwargs,
    ):
        """Initialize the XTTS service.

        Args:
            voice_id: ID of the voice/speaker to use for synthesis.
            base_url: Base URL of the XTTS streaming server.
            aiohttp_session: HTTP session for making requests to the server.
            language: Language for synthesis. Defaults to English.
            sample_rate: Audio sample rate. If None, uses default.
            **kwargs: Additional arguments passed to parent TTSService.
        """
        super().__init__(sample_rate=sample_rate, **kwargs)

        self._settings = {
            "language": self.language_to_service_language(language),
            "base_url": base_url,
        }
        self.set_voice(voice_id)
        self._studio_speakers: Optional[Dict[str, Any]] = None
        self._aiohttp_session = aiohttp_session

        self._resampler = create_stream_resampler()

    def can_generate_metrics(self) -> bool:
        """Check if this service can generate processing metrics.

        Returns:
            True, as XTTS service supports metrics generation.
        """
        return True

    def language_to_service_language(self, language: Language) -> Optional[str]:
        """Convert a Language enum to XTTS service language format.

        Args:
            language: The language to convert.

        Returns:
            The XTTS-specific language code, or None if not supported.
        """
        return language_to_xtts_language(language)

    async def start(self, frame: StartFrame):
        """Start the XTTS service and load studio speakers.

        Args:
            frame: The start frame containing initialization parameters.
        """
        await super().start(frame)

        if self._studio_speakers:
            return

        async with self._aiohttp_session.get(self._settings["base_url"] + "/studio_speakers") as r:
            if r.status != 200:
                text = await r.text()
                logger.error(
                    f"{self} error getting studio speakers (status: {r.status}, error: {text})"
                )
                await self.push_error(
                    ErrorFrame(
                        f"Error error getting studio speakers (status: {r.status}, error: {text})"
                    )
                )
                return
            self._studio_speakers = await r.json()

    @traced_tts
    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        """Generate speech from text using XTTS streaming server.

        Args:
            text: The text to synthesize into speech.

        Yields:
            Frame: Audio frames containing the synthesized speech.
        """
        logger.debug(f"{self}: Generating TTS [{text}]")

        if not self._studio_speakers:
            logger.error(f"{self} no studio speakers available")
            return

        embeddings = self._studio_speakers[self._voice_id]

        url = self._settings["base_url"] + "/tts_stream"

        payload = {
            "text": text.replace(".", "").replace("*", ""),
            "language": self._settings["language"],
            "speaker_embedding": embeddings["speaker_embedding"],
            "gpt_cond_latent": embeddings["gpt_cond_latent"],
            "add_wav_header": False,
            "stream_chunk_size": 20,
        }

        await self.start_ttfb_metrics()

        async with self._aiohttp_session.post(url, json=payload) as r:
            if r.status != 200:
                text = await r.text()
                logger.error(f"{self} error getting audio (status: {r.status}, error: {text})")
                yield ErrorFrame(f"Error getting audio (status: {r.status}, error: {text})")
                return

            await self.start_tts_usage_metrics(text)

            yield TTSStartedFrame()

            CHUNK_SIZE = self.chunk_size

            buffer = bytearray()
            async for chunk in r.content.iter_chunked(CHUNK_SIZE):
                if len(chunk) > 0:
                    await self.stop_ttfb_metrics()
                    # Append new chunk to the buffer.
                    buffer.extend(chunk)

                    # Check if buffer has enough data for processing.
                    while (
                        len(buffer) >= 48000
                    ):  # Assuming at least 0.5 seconds of audio data at 24000 Hz
                        # Process the buffer up to a safe size for resampling.
                        process_data = buffer[:48000]
                        # Remove processed data from buffer.
                        buffer = buffer[48000:]

                        # XTTS uses 24000 so we need to resample to our desired rate.
                        resampled_audio = await self._resampler.resample(
                            bytes(process_data), 24000, self.sample_rate
                        )
                        # Create the frame with the resampled audio
                        frame = TTSAudioRawFrame(resampled_audio, self.sample_rate, 1)
                        yield frame

            # Process any remaining data in the buffer.
            if len(buffer) > 0:
                resampled_audio = await self._resampler.resample(
                    bytes(buffer), 24000, self.sample_rate
                )
                frame = TTSAudioRawFrame(resampled_audio, self.sample_rate, 1)
                yield frame

            yield TTSStoppedFrame()
