#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import time
from abc import abstractmethod
from typing import Dict, Optional

import numpy as np
from loguru import logger
from pydantic import BaseModel

from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer, EndOfTurnState

# Default timing parameters
STOP_SECS = 3
PRE_SPEECH_MS = 0
MAX_DURATION_SECONDS = 8  # Max allowed segment duration
USE_ONLY_LAST_VAD_SEGMENT = True


class SmartTurnParams(BaseModel):
    stop_secs: float = STOP_SECS
    pre_speech_ms: float = PRE_SPEECH_MS
    max_duration_secs: float = MAX_DURATION_SECONDS
    # not exposing this for now yet until the model can handle it.
    # use_only_last_vad_segment: bool = USE_ONLY_LAST_VAD_SEGMENT


class BaseSmartTurn(BaseTurnAnalyzer):
    def __init__(
        self, *, sample_rate: Optional[int] = None, params: SmartTurnParams = SmartTurnParams()
    ):
        super().__init__(sample_rate=sample_rate)
        self._params = params
        # Configuration
        self._stop_ms = self._params.stop_secs * 1000  # silence threshold in ms
        # Inference state
        self._audio_buffer = []
        self._speech_triggered = False
        self._silence_ms = 0
        self._speech_start_time = None

    @property
    def speech_triggered(self) -> bool:
        return self._speech_triggered

    def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
        # Convert raw audio to float32 format and append to the buffer
        audio_int16 = np.frombuffer(buffer, dtype=np.int16)
        audio_float32 = np.frombuffer(audio_int16, dtype=np.int16).astype(np.float32) / 32768.0
        self._audio_buffer.append((time.time(), audio_float32))

        state = EndOfTurnState.INCOMPLETE

        if is_speech:
            # Reset silence tracking on speech
            self._silence_ms = 0
            self._speech_triggered = True
            if self._speech_start_time is None:
                self._speech_start_time = time.time()
                logger.debug(f"Speech started at {self._speech_start_time}")
        else:
            if self._speech_triggered:
                chunk_duration_ms = len(audio_int16) / (self._sample_rate / 1000)
                self._silence_ms += chunk_duration_ms
                # If silence exceeds threshold, mark end of turn
                if self._silence_ms >= self._stop_ms:
                    logger.debug(
                        f"End of Turn complete due to stop_secs. Silence in ms: {self._silence_ms}"
                    )
                    state = EndOfTurnState.COMPLETE
                    self._clear(state)
            else:
                # Trim buffer to prevent unbounded growth before speech
                max_buffer_time = (
                    (self._params.pre_speech_ms / 1000)
                    + self._params.stop_secs
                    + self._params.max_duration_secs
                )
                while (
                    self._audio_buffer and self._audio_buffer[0][0] < time.time() - max_buffer_time
                ):
                    self._audio_buffer.pop(0)

        return state

    def analyze_end_of_turn(self) -> EndOfTurnState:
        logger.debug("Analyzing End of Turn...")
        state = self._process_speech_segment(self._audio_buffer)
        if state == EndOfTurnState.COMPLETE or USE_ONLY_LAST_VAD_SEGMENT:
            self._clear(state)
        logger.debug(f"End of Turn result: {state}")
        return state

    def _clear(self, turn_state: EndOfTurnState):
        # Reset internal state for next turn
        logger.debug("Clearing audio buffer...")
        # If the state is still incomplete, keep the _speech_triggered as True
        self._speech_triggered = turn_state == EndOfTurnState.INCOMPLETE
        self._audio_buffer = []
        self._speech_start_time = None
        self._silence_ms = 0

    def _process_speech_segment(self, audio_buffer) -> EndOfTurnState:
        state = EndOfTurnState.INCOMPLETE

        if not audio_buffer:
            return state

        # Extract recent audio segment for prediction
        start_time = self._speech_start_time - (self._params.pre_speech_ms / 1000)
        start_index = 0
        for i, (t, _) in enumerate(audio_buffer):
            if t >= start_time:
                start_index = i
                break

        end_index = len(audio_buffer) - 1

        # Extract the audio segment
        segment_audio_chunks = [chunk for _, chunk in audio_buffer[start_index : end_index + 1]]
        segment_audio = np.concatenate(segment_audio_chunks)

        logger.debug(f"Segment audio chunks after start index: {len(segment_audio)}")

        # Limit maximum duration
        max_samples = int(self._params.max_duration_secs * self.sample_rate)
        if len(segment_audio) > max_samples:
            # slices the array to keep the last max_samples samples, discarding the earlier part.
            segment_audio = segment_audio[-max_samples:]

        logger.debug(f"Segment audio chunks after limiting duration: {len(segment_audio)}")

        if len(segment_audio) > 0:
            start_time = time.perf_counter()
            result = self._predict_endpoint(segment_audio)
            state = (
                EndOfTurnState.COMPLETE if result["prediction"] == 1 else EndOfTurnState.INCOMPLETE
            )
            end_time = time.perf_counter()

            logger.debug("--------")
            logger.debug(f"Prediction: {'Complete' if result['prediction'] == 1 else 'Incomplete'}")
            logger.debug(f"Probability of complete: {result['probability']:.4f}")
            logger.debug(f"Prediction took {(end_time - start_time) * 1000:.2f}ms seconds")
        else:
            logger.debug(f"params: {self._params}, stop_ms: {self._stop_ms}")
            logger.debug("Captured empty audio segment, skipping prediction.")

        return state

    @abstractmethod
    def _predict_endpoint(self, buffer: np.ndarray) -> Dict[str, any]:
        """
        Abstract method to predict if a turn has ended based on audio.

        Args:
            buffer: Float32 numpy array of audio samples at 16kHz.

        Returns:
            Dictionary with:
              - prediction: 1 if turn is complete, else 0
              - probability: Confidence of the prediction
        """
        pass
