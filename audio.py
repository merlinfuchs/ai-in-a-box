import sounddevice as sd
import asyncio
import time
import threading

import numpy as np

CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 20
QUEUE_MAXSIZE = 300
GAIN_RAMP_MS = 30
INITIAL_GAIN = 1.0


class AudioInput:
    def __init__(self, device_index: int, required_sample_rate: int):
        self.device_index = device_index
        self.required_sample_rate = required_sample_rate
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.loop = asyncio.get_running_loop()

        self.device_sample_rate = pick_input_sr(device_index, required_sample_rate)
        frames_per_block = int(self.device_sample_rate * BLOCK_MS / 1000)

        self.stream = sd.InputStream(
            device=device_index,
            samplerate=self.device_sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=frames_per_block,
            callback=self._callback,
        )

    @classmethod
    def from_name_substring(
        cls, name_substring: str, required_sample_rate: int
    ) -> "AudioInput":
        device_index = find_input_device(name_substring)
        return cls(device_index, required_sample_rate)

    @classmethod
    def system_default(cls, required_sample_rate: int) -> "AudioInput":
        """Get the system default input device."""
        default_input_device = sd.default.device[0]
        if default_input_device is None:
            raise RuntimeError("No default input device is set in the system.")
        return cls(default_input_device, required_sample_rate)

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()

    def close(self):
        self.stream.close()

    def _callback(self, indata: np.ndarray, frames, time, status):
        if status:
            # Over/underflows are common; not fatal
            pass

        # Convert to mono if stereo
        if indata.ndim == 2:
            indata = indata[:, 0]

        # Copy the buffer; PortAudio reuses it
        indata = indata.copy()

        if self.device_sample_rate != self.required_sample_rate:
            indata = resample_int16_mono(
                indata, self.device_sample_rate, self.required_sample_rate
            )

        def _try_put():
            if self.queue.full():
                print("Audio input queue full, dropping audio")
                return
            self.queue.put_nowait(indata)

        self.loop.call_soon_threadsafe(_try_put)

    async def get(self) -> bytes:
        return await self.queue.get()

    def drain(self):
        while not self.queue.empty():
            self.queue.get_nowait()


class AudioOutput:
    def __init__(self, device_index: int, required_sample_rate: int):
        self.device_index = device_index
        self.required_sample_rate = required_sample_rate
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.loop = asyncio.get_running_loop()
        self.stopped = threading.Event()
        self.worker_thread = None

        self._gain_lock = threading.Lock()
        self._gain_current = float(INITIAL_GAIN)
        self._gain_target = float(INITIAL_GAIN)

        self.device_sample_rate = pick_output_sr(device_index, required_sample_rate)
        self.stream = sd.OutputStream(
            device=device_index,
            samplerate=self.device_sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
        )

    @classmethod
    def from_name_substring(
        cls, name_substring: str, required_sample_rate: int
    ) -> "AudioOutput":
        device_index = find_output_device(name_substring)
        return cls(device_index, required_sample_rate)

    @classmethod
    def system_default(cls, required_sample_rate: int) -> "AudioOutput":
        """Get the system default output device."""
        default_output_device = sd.default.device[1]
        if default_output_device is None:
            raise RuntimeError("No default output device is set in the system.")
        return cls(default_output_device, required_sample_rate)

    def start(self):
        self.stopped.clear()
        self.stream.start()
        threading.Thread(target=self._worker).start()

    def stop(self):
        self.stopped.set()
        self.stream.stop()

    def close(self):
        self.stopped.set()
        self.stream.close()

    def is_empty(self) -> bool:
        return self.queue.empty()

    async def wait_for_empty(self):
        while not self.queue.empty():
            await asyncio.sleep(0.01)

    async def write(self, data: np.ndarray):
        if self.device_sample_rate != self.required_sample_rate:
            data = resample_int16_mono(
                data, self.device_sample_rate, self.required_sample_rate
            )

        await self.queue.put(data)

    def set_volume(self, volume: float):
        with self._gain_lock:
            self._gain_target = float(volume)

    def drain(self):
        while not self.queue.empty():
            self.queue.get_nowait()

    def _worker(self):
        while not self.stopped.is_set():
            try:
                data = self.queue.get_nowait()
                data = self._apply_gain_ramped_int16(data)
                self.stream.write(data)
            except asyncio.QueueEmpty:
                time.sleep(0.01)

    def _apply_gain_ramped_int16(self, x_int16: np.ndarray) -> np.ndarray:
        """
        Apply gain with a linear ramp from current -> target to avoid clicks.
        """
        if x_int16.size == 0:
            return x_int16

        with self._gain_lock:
            target = self._gain_target
            current = self._gain_current

        if target == current:
            # Fast path
            if current == 1.0:
                return x_int16
            y = x_int16.astype(np.float32) * current
            np.clip(y, -32768, 32767, out=y)
            return y.astype(np.int16)

        # Compute how many samples we want to spend ramping
        ramp_samples = int(self.device_sample_rate * (GAIN_RAMP_MS / 1000.0))
        ramp_samples = max(1, ramp_samples)

        n = x_int16.shape[0]
        k = min(n, ramp_samples)

        x = x_int16.astype(np.float32)

        # Create gain curve:
        # - First k samples ramp linearly current -> target
        # - Remaining samples (if any) use target
        gains = np.empty(n, dtype=np.float32)
        gains[:k] = np.linspace(current, target, num=k, dtype=np.float32)
        if k < n:
            gains[k:] = target

        y = x * gains
        np.clip(y, -32768, 32767, out=y)

        # Update current gain to the last used value (target if ramp completed)
        with self._gain_lock:
            self._gain_current = float(gains[-1])

        return y.astype(np.int16)


def resample_int16_mono(
    x_int16: np.ndarray, src_rate: int, dst_rate: int
) -> np.ndarray:
    if src_rate == dst_rate:
        return x_int16
    x = x_int16.astype(np.float32)
    n_src = x.shape[0]
    n_dst = int(round(n_src * (dst_rate / src_rate)))
    if n_src < 2 or n_dst < 2:
        return x_int16
    src_idx = np.linspace(0, n_src - 1, num=n_src, dtype=np.float32)
    dst_idx = np.linspace(0, n_src - 1, num=n_dst, dtype=np.float32)
    y = np.interp(dst_idx, src_idx, x).astype(np.int16)
    return y


def apply_volume_int16(x: np.ndarray, volume: float) -> np.ndarray:
    if volume == 1.0:
        return x
    y = x.astype(np.float32) * volume
    np.clip(y, -32768, 32767, out=y)
    return y.astype(np.int16)


def find_input_device(name_substring: str) -> int:
    devices = sd.query_devices()
    for idx, d in enumerate(devices):
        if (
            d.get("max_input_channels", 0) > 0
            and name_substring.lower() in d.get("name", "").lower()
        ):
            return idx
    raise RuntimeError(
        f'Could not find an input device containing "{name_substring}". '
        'Run: python -c "import sounddevice as sd; print(sd.query_devices())"'
    )


def find_output_device(name_substring: str) -> int:
    devices = sd.query_devices()
    for idx, d in enumerate(devices):
        if (
            d.get("max_output_channels", 0) > 0
            and name_substring.lower() in d.get("name", "").lower()
        ):
            return idx
    raise RuntimeError(
        f'Could not find an output device containing "{name_substring}". '
        'Run: python -c "import sounddevice as sd; print(sd.query_devices())"'
    )


def get_device_default_sr(device_index: int) -> int:
    d = sd.query_devices(device_index)
    return int(round(float(d.get("default_samplerate", 48000.0))))


def pick_output_sr(device_index: int, required: int) -> int:
    candidates = [required, get_device_default_sr(device_index), 48000, 44100]
    for sr in candidates:
        try:
            sd.check_output_settings(
                device=device_index, samplerate=sr, channels=CHANNELS, dtype=DTYPE
            )
            return sr
        except Exception:
            continue
    raise RuntimeError(f"No supported output sample rate for device {device_index}")


def pick_input_sr(device_index: int, required: int) -> int:
    candidates = [
        required,
        get_device_default_sr(device_index),
        48000,
        44100,
        32000,
        16000,
    ]
    for sr in candidates:
        try:
            sd.check_input_settings(
                device=device_index, samplerate=sr, channels=CHANNELS, dtype=DTYPE
            )
            return sr
        except Exception:
            continue
    raise RuntimeError(f"No supported input sample rate for device {device_index}")
