from typing import Awaitable, Callable

import numpy as np
from agents import function_tool
from agents.realtime import (
    RealtimeAgent,
    RealtimeRunner,
    RealtimeSession,
    RealtimeSessionEvent,
)
from agents.realtime.model import RealtimeModelConfig
import asyncio


@function_tool
async def get_weather(city: str) -> str:
    """Get the weather in a city."""
    return f"The weather in {city} is snowy."


class Agent:
    session: RealtimeSession | None = None
    _audio_callback: Callable[[np.ndarray], Awaitable[None]] | None = None
    _transcription_callback: Callable[[str, str], Awaitable[None]] | None = None
    _interrupt_callback: Callable[[], Awaitable[None]] | None = None

    def __init__(
        self, language: str, agent_name: str, instructions: str, voice: str = "cedar"
    ):
        self.language = language
        self.voice = voice
        self.runner = RealtimeRunner(
            RealtimeAgent(
                name=agent_name,
                instructions=instructions + f"\n\n Only respond in {self.language}!",
                # tools=[get_weather],
            )
        )
        self.session = None

    async def start(
        self,
        audio_callback: Callable[[np.ndarray], Awaitable[None]],
        transcription_callback: Callable[[str, str], Awaitable[None]],
        interrupt_callback: Callable[[], Awaitable[None]],
    ):
        self._audio_callback = audio_callback
        self._transcription_callback = transcription_callback
        self._interrupt_callback = interrupt_callback

        model_config: RealtimeModelConfig = {
            "initial_model_settings": {
                "voice": self.voice,
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": 200,
                    "interrupt_response": True,
                    "create_response": True,
                },
                "input_audio_transcription": {
                    "model": "gpt-4o-mini-transcribe",
                    "language": self.language,
                },
            },
        }
        self.session = await self.runner.run(model_config=model_config)
        await self.session.enter()
        asyncio.create_task(self._process_events())
        print("Agent started")

    async def stop(self):
        if self.session is not None:
            await self.session.close()
        self.session = None

    async def send_audio(self, audio: np.ndarray):
        if self.session is None:
            return

        print(f"Sending audio {len(audio)}")
        # await self._audio_callback(audio)
        await self.session.send_audio(audio.tobytes())

    async def send_message(self, prompt: str):
        if self.session is None:
            return

        print(f"Sending message: {prompt}")
        await self.session.send_message(prompt)

    async def _process_events(self):
        async for event in self.session:
            await self._on_event(event)

    async def _on_event(
        self,
        event: RealtimeSessionEvent,
    ):
        if event.type == "agent_start":
            print(f"Agent started: {event.agent.name}")
        elif event.type == "agent_end":
            print(f"Agent ended: {event.agent.name}")
        elif event.type == "handoff":
            print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
        elif event.type == "tool_start":
            print(f"Tool started: {event.tool.name}")
        elif event.type == "tool_end":
            print(f"Tool ended: {event.tool.name}; output: {event.output}")
        elif event.type == "audio_end":
            print("Audio ended")
        elif event.type == "audio":
            np_audio = np.frombuffer(event.audio.data, dtype=np.int16)
            await self._audio_callback(np_audio)
        elif event.type == "audio_interrupted":
            print("Audio interrupted")
            await self._interrupt_callback()
        elif event.type == "error":
            print(f"Error: {event.error}")
        elif event.type == "history_updated":
            pass  # Skip these frequent events
        elif event.type == "history_added":
            pass  # Skip these frequent events
        elif event.type == "raw_model_event":
            if hasattr(event.data, "data") and isinstance(event.data.data, dict):
                data = event.data.data
                event_type = data.get("type")

                if (
                    event_type
                    == "conversation.item.input_audio_transcription.completed"
                ):
                    transcript = data.get("transcript")
                    await self._transcription_callback("input", transcript)

                elif event_type == "response.output_audio_transcript.done":
                    transcript = data.get("transcript")
                    await self._transcription_callback("output", transcript)
        else:
            print(f"Unknown event type: {event.type}")
