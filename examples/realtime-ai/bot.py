#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import sys
import os

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frameworks.realtimeai import (
    RealtimeAIConfig,
    RealtimeAILLMConfig,
    RealtimeAIProcessor,
    RealtimeAISetup,
    RealtimeAITTSConfig)
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.vad.silero import SileroVADAnalyzer

from runner import configure

from loguru import logger

from dotenv import load_dotenv
load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


async def main(room_url, token):
    transport = DailyTransport(
        room_url,
        token,
        "Realtime AI",
        DailyParams(
            audio_out_enabled=True,
            transcription_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer()
        ))

    llm = RealtimeAILLMConfig(
        model="llama3-70b-8192",
        messages=[{"role": "system", "content": "You are a helpful assistant named Gary. Briefly say hello!"}]
    )
    tts = RealtimeAITTSConfig(voice="79a125e8-cd45-4c13-8a67-188112f4dd22")
    setup = RealtimeAISetup(config=RealtimeAIConfig(llm=llm, tts=tts))

    rtai = RealtimeAIProcessor(
        transport=transport,
        setup=setup,
        llm_api_key=os.getenv("OPENAI_API_KEY"),
        tts_api_key=os.getenv("CARTESIA_API_KEY"))

    runner = PipelineRunner()

    pipeline = Pipeline([transport.input(), rtai])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            report_only_initial_ttfb=True))

    @transport.event_handler("on_participant_joined")
    async def on_participant_joined(transport, participant):
        transport.capture_participant_transcription(participant["id"])

    await runner.run(task)

if __name__ == "__main__":
    (url, token) = configure()
    asyncio.run(main(url, token))