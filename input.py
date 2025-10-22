from dataclasses import dataclass
from typing import Callable
from abc import ABC, abstractmethod

import asyncio


@dataclass
class InputCallbacks:
    on_talk_pressed: Callable[[], None]
    on_talk_released: Callable[[], None]
    on_mode_pressed: Callable[[int], None]
    on_reset_pressed: Callable[[], None]
    on_power_pressed: Callable[[], None]
    on_power_released: Callable[[], None]


class InputBackend(ABC):
    @abstractmethod
    def setup(self, _: InputCallbacks):
        raise NotImplementedError

    @abstractmethod
    def cleanup(self):
        raise NotImplementedError

    @abstractmethod
    def indicator_on(self):
        raise NotImplementedError

    @abstractmethod
    def indicator_off(self):
        raise NotImplementedError

    @abstractmethod
    def indicator_set_color(self, color: tuple[int, int, int]):
        raise NotImplementedError

    @abstractmethod
    def is_power_pressed(self) -> bool:
        raise NotImplementedError


class InputBackendGPIO(InputBackend):
    def __init__(
        self,
        talk_bcm: int,
        mode_bcm: int,
        reset_bcm: int,
        power_bcm: int,
        indicator_red_bcm: int,
        indicator_green_bcm: int,
        indicator_blue_bcm: int,
    ):
        from gpiozero import Button, RGBLED, Device
        from gpiozero.pins.lgpio import LGPIOFactory

        Device.pin_factory = LGPIOFactory()

        self.loop = asyncio.get_running_loop()
        self.talk_button = Button(talk_bcm, pull_up=False, bounce_time=0.05)
        self.mode_button = Button(mode_bcm, pull_up=False, bounce_time=0.05)
        self.reset_button = Button(reset_bcm, pull_up=False, bounce_time=0.05)
        self.power_button = Button(power_bcm, pull_up=True, bounce_time=0.05)
        self.indicator = RGBLED(
            red=indicator_red_bcm,
            green=indicator_green_bcm,
            blue=indicator_blue_bcm,
        )

    def setup(self, callbacks: InputCallbacks):
        self.talk_button.when_pressed = lambda: self.loop.call_soon_threadsafe(
            callbacks.on_talk_pressed
        )
        self.talk_button.when_released = lambda: self.loop.call_soon_threadsafe(
            callbacks.on_talk_released
        )
        self.mode_button.when_pressed = lambda: self.loop.call_soon_threadsafe(
            callbacks.on_mode_pressed
        )
        self.reset_button.when_pressed = lambda: self.loop.call_soon_threadsafe(
            callbacks.on_reset_pressed
        )
        self.power_button.when_pressed = lambda: self.loop.call_soon_threadsafe(
            callbacks.on_power_pressed
        )
        self.power_button.when_released = lambda: self.loop.call_soon_threadsafe(
            callbacks.on_power_released
        )

    def cleanup(self):
        self.talk_button.close()
        self.mode_button.close()
        self.reset_button.close()
        self.power_button.close()

    def indicator_on(self):
        self.indicator.on()

    def indicator_off(self):
        self.indicator.off()

    def indicator_set_color(self, color: tuple[int, int, int]):
        self.indicator.color = color

    def is_power_pressed(self) -> bool:
        return self.power_button.is_active


class InputBackendKeyboard(InputBackend):
    def __init__(
        self,
        talk_key: str,
        mode_key: str,
        reset_key: str,
        power_key: str,
    ):
        self.loop = asyncio.get_running_loop()
        self.talk_key = talk_key
        self.mode_key = mode_key
        self.reset_key = reset_key
        self.power_key = power_key
        self.listener = None

    def setup(self, callbacks: InputCallbacks):
        from pynput import keyboard

        ctrl_pressed = False
        pressed_keys = set()

        def on_press(key):
            nonlocal ctrl_pressed
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                ctrl_pressed = True
            elif ctrl_pressed and hasattr(key, "char"):
                if key.char == self.talk_key:
                    if "talk" not in pressed_keys:
                        pressed_keys.add("talk")
                        self.loop.call_soon_threadsafe(callbacks.on_talk_pressed)
                elif key.char == self.mode_key:
                    if "mode" not in pressed_keys:
                        pressed_keys.add("mode")
                        self.loop.call_soon_threadsafe(callbacks.on_mode_pressed)
                elif key.char == self.reset_key:
                    if "reset" not in pressed_keys:
                        pressed_keys.add("reset")
                        self.loop.call_soon_threadsafe(callbacks.on_reset_pressed)
                elif key.char == self.power_key:
                    if "power" not in pressed_keys:
                        pressed_keys.add("power")
                        self.loop.call_soon_threadsafe(callbacks.on_power_pressed)

        def on_release(key):
            nonlocal ctrl_pressed
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                ctrl_pressed = False
            elif hasattr(key, "char"):
                if key.char == self.talk_key:
                    pressed_keys.discard("talk")
                    self.loop.call_soon_threadsafe(callbacks.on_talk_released)
                elif key.char == self.mode_key:
                    pressed_keys.discard("mode")
                elif key.char == self.reset_key:
                    pressed_keys.discard("reset")
                elif key.char == self.power_key:
                    pressed_keys.discard("power")
                    self.loop.call_soon_threadsafe(callbacks.on_power_released)

        self.listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.listener.start()

    def cleanup(self):
        print("Stopping keyboard listener")
        self.listener.stop()

    def indicator_on(self):
        pass

    def indicator_off(self):
        pass

    def indicator_set_color(self, color: tuple[int, int, int]):
        pass

    def is_power_pressed(self) -> bool:
        return True


def gpio_available() -> bool:
    try:
        from gpiozero import Device
        from gpiozero.pins.native import NativeFactory

        Device.pin_factory = NativeFactory()
        return True
    except Exception:
        return False
