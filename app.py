from input import InputBackend, InputCallbacks
from audio import AudioInput, AudioOutput
import asyncio
from agent import Agent
import numpy as np
import traceback
from enum import Enum
import threading


class AppMode(Enum):
    NEUTRAL = "neutral"
    SUPPORTER = "supporter"
    CRITIC = "critic"
    DEBATE = "debate"


class CurrentAgent(Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class AppState:
    _mode_lock: threading.Lock
    _mode: AppMode

    _current_agent_lock: threading.Lock
    _current_agent: CurrentAgent

    _input_active: asyncio.Event
    _deactivate_input_timer: asyncio.TimerHandle

    def __init__(self):
        self._mode_lock = threading.Lock()
        self._mode = AppMode.NEUTRAL
        self._current_agent_lock = threading.Lock()
        self._current_agent = CurrentAgent.PRIMARY
        self._input_active = asyncio.Event()
        self._deactivate_input_timer = None

    @property
    def mode(self) -> AppMode:
        with self._mode_lock:
            return self._mode

    @mode.setter
    def mode(self, mode: AppMode):
        with self._mode_lock:
            self._mode = mode

    @property
    def current_agent(self) -> CurrentAgent:
        with self._current_agent_lock:
            return self._current_agent

    @current_agent.setter
    def current_agent(self, current_agent: CurrentAgent):
        with self._current_agent_lock:
            self._current_agent = current_agent

    @property
    def input_active(self) -> bool:
        return self._input_active.is_set()

    def activate_input(self):
        if self._deactivate_input_timer:
            self._deactivate_input_timer.cancel()
            self._deactivate_input_timer = None

        self._input_active.set()

    def deactivate_input(self):
        if self._deactivate_input_timer:
            self._deactivate_input_timer.cancel()
            self._deactivate_input_timer = None

        self._input_active.clear()

    def deactivate_input_after(self, seconds: float):
        if self._deactivate_input_timer:
            self._deactivate_input_timer.cancel()
            self._deactivate_input_timer = None

        loop = asyncio.get_running_loop()
        self._deactivate_input_timer = loop.call_later(seconds, self.deactivate_input)

    async def wait_for_input_active(self):
        await self._input_active.wait()


class App:
    def __init__(
        self,
        input_backend: InputBackend,
        audio_input: AudioInput,
        audio_output: AudioOutput,
        neutral_agent: Agent,
        supporter_agent: Agent,
        critic_agent: Agent,
    ):
        self.loop = asyncio.get_running_loop()
        self.input_backend = input_backend
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.neutral_agent = neutral_agent
        self.supporter_agent = supporter_agent
        self.critic_agent = critic_agent

        self.state = AppState()
        self.stopped = asyncio.Event()
        self.stopped.set()

        self.primary_agent = self._primary_agent_for_mode()
        self.secondary_agent = self._secondary_agent_for_mode()

    @property
    def current_agent(self) -> Agent:
        match self.state.current_agent:
            case CurrentAgent.PRIMARY:
                return self.primary_agent
            case CurrentAgent.SECONDARY if self.secondary_agent:
                return self.secondary_agent
            case _:
                return self.primary_agent

    async def setup(self):
        self.input_backend.setup(
            InputCallbacks(
                on_talk_pressed=self._on_talk_pressed,
                on_talk_released=self._on_talk_released,
                on_mode_pressed=self._on_mode_pressed,
                on_reset_pressed=self._on_reset_pressed,
                on_power_pressed=self._on_power_pressed,
                on_power_released=self._on_power_released,
            )
        )

    async def start(self):
        if not self.stopped.is_set():
            return

        if not self.input_backend.is_power_pressed():
            return

        self.primary_agent = self._primary_agent_for_mode()
        self.secondary_agent = self._secondary_agent_for_mode()
        self.state.current_agent = CurrentAgent.PRIMARY

        self.stopped.clear()
        self.audio_output.start()
        self.audio_input.start()

        asyncio.create_task(self._forward_input_audio())
        asyncio.create_task(self._adjust_output_volume())
        asyncio.create_task(self._adjust_indicator_state())

        await self.primary_agent.start(
            self._forward_primary_output_audio,
            self._on_primary_transcription,
            self._interrupt_primary_output_audio,
        )
        if self.secondary_agent:
            await self.secondary_agent.start(
                self._forward_secondary_output_audio,
                self._on_secondary_transcription,
                self._interrupt_secondary_output_audio,
            )

    async def stop(self):
        if self.stopped.is_set():
            return

        print("Stopping app")
        self.stopped.set()
        self.audio_output.stop()
        self.audio_output.drain()
        self.audio_input.stop()
        self.audio_input.drain()

        await self.primary_agent.stop()
        if self.secondary_agent:
            await self.secondary_agent.stop()

    async def close(self):
        self.stopped.set()
        self.audio_output.stop()
        self.audio_input.stop()
        self.audio_output.close()
        self.audio_input.close()
        self.input_backend.cleanup()
        self.input_backend.indicator_off()
        await self.primary_agent.stop()
        if self.secondary_agent:
            await self.secondary_agent.stop()

    async def switch_mode(self, mode: AppMode):
        await self.stop()
        self.state.mode = mode
        await self.start()

    async def _forward_input_audio(self):
        while not self.stopped.is_set():
            audio = await self.audio_input.get()

            if not self.state.input_active:
                continue

            try:
                await self.current_agent.send_audio(audio)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error forwarding input audio: {e}")
                traceback.print_exc()
                continue

    async def _forward_primary_output_audio(self, audio: np.ndarray):
        print(f"Audio received from primary agent {audio.shape}")
        if self.state.current_agent == CurrentAgent.SECONDARY:
            print("Skipping forwarding output audio from primary agent to output")
            return

        try:
            await self.audio_output.write(audio)
        except Exception as e:
            print(f"Error forwarding output audio: {e}")
            traceback.print_exc()

    async def _forward_secondary_output_audio(self, audio: np.ndarray):
        print(f"Audio received from secondary agent {audio.shape}")
        if self.state.current_agent == CurrentAgent.PRIMARY:
            print("Skipping forwarding output audio from secondary agent to output")
            return

        try:
            await self.audio_output.write(audio)
        except Exception as e:
            print(f"Error forwarding output audio: {e}")
            traceback.print_exc()

    async def _on_primary_transcription(self, role: str, transcript: str):
        print(f"Transcription from primary agent ({role}): {transcript}")
        if (
            role == "output"
            and self.state.mode == AppMode.DEBATE
            and self.state.current_agent == CurrentAgent.PRIMARY
        ):
            await self.audio_output.wait_for_empty()
            self.state.current_agent = CurrentAgent.SECONDARY
            await self.current_agent.send_message(transcript)

    async def _on_secondary_transcription(self, role: str, transcript: str):
        print(f"Transcription from secondary agent ({role}): {transcript}")
        if (
            role == "output"
            and self.state.mode == AppMode.DEBATE
            and self.state.current_agent == CurrentAgent.SECONDARY
        ):
            await self.audio_output.wait_for_empty()
            self.state.current_agent = CurrentAgent.PRIMARY
            await self.current_agent.send_message(transcript)

    async def _interrupt_primary_output_audio(self):
        print("Interrupting primary output audio")
        self.audio_output.drain()

    async def _interrupt_secondary_output_audio(self):
        print("Interrupting secondary output audio")
        self.audio_output.drain()

    async def _adjust_output_volume(self):
        while not self.stopped.is_set():
            if self.state.input_active:
                self.audio_output.set_volume(0.2)
            else:
                self.audio_output.set_volume(1.0)
            await asyncio.sleep(0.1)

    async def _adjust_indicator_state(self):
        while not self.stopped.is_set():
            if self.state.input_active:
                self.input_backend.indicator_off()
            elif not self.audio_output.is_empty():
                self._indicator_on_with_mode_color()
                await asyncio.sleep(0.3)
                self.input_backend.indicator_off()
                await asyncio.sleep(0.2)
            else:
                self._indicator_on_with_mode_color()

            await asyncio.sleep(0.1)

        self.input_backend.indicator_off()

    def _indicator_on_with_mode_color(self):
        match self.state.mode:
            case AppMode.NEUTRAL:
                self.input_backend.indicator_set_color((0, 0, 1))  # Blue
            case AppMode.SUPPORTER:
                self.input_backend.indicator_set_color((0, 1, 0))  # Green
            case AppMode.CRITIC:
                self.input_backend.indicator_set_color((1, 0, 0))  # Red
            case AppMode.DEBATE:
                if self.audio_output.is_empty():
                    self.input_backend.indicator_set_color((1, 1, 0))  # Yellow
                else:
                    match self.state.current_agent:
                        case CurrentAgent.PRIMARY:
                            self.input_backend.indicator_set_color((0, 0, 1))  # Blue
                        case CurrentAgent.SECONDARY:
                            self.input_backend.indicator_set_color((1, 0, 0))  # Red
                        case _:
                            self.input_backend.indicator_set_color((1, 1, 1))  # White
            case _:
                self.input_backend.indicator_set_color((1, 1, 1))  # White

    def _primary_agent_for_mode(self) -> Agent:
        match self.state.mode:
            case AppMode.NEUTRAL:
                return self.neutral_agent
            case AppMode.SUPPORTER:
                return self.supporter_agent
            case AppMode.CRITIC:
                return self.critic_agent
            case AppMode.DEBATE:
                return self.supporter_agent
            case _:
                return self.neutral_agent

    def _secondary_agent_for_mode(self) -> Agent | None:
        if self.state.mode == AppMode.DEBATE:
            return self.critic_agent
        return None

    def _on_talk_pressed(self):
        print("Talk pressed")
        self.state.activate_input()

    def _on_talk_released(self):
        print("Talk released")
        self.state.deactivate_input_after(1)

    def _on_mode_pressed(self):
        print("Mode pressed")
        new_mode = None
        match self.state.mode:
            case AppMode.NEUTRAL:
                new_mode = AppMode.SUPPORTER
            case AppMode.SUPPORTER:
                new_mode = AppMode.CRITIC
            case AppMode.CRITIC:
                new_mode = AppMode.DEBATE
            case AppMode.DEBATE:
                new_mode = AppMode.NEUTRAL
            case _:
                new_mode = AppMode.NEUTRAL

        print(f"Switching to mode {new_mode}")
        asyncio.create_task(self.switch_mode(new_mode))

    def _on_reset_pressed(self):
        print("Reset pressed")

        async def _reset():
            await self.stop()
            await self.start()

        asyncio.create_task(_reset())

    def _on_power_pressed(self):
        print("Power pressed")
        asyncio.create_task(self.start())

    def _on_power_released(self):
        print("Power released")
        asyncio.create_task(self.stop())
