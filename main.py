import config
from input import (
    gpio_available,
    InputBackendGPIO,
    InputBackendKeyboard,
)
from audio import AudioInput, AudioOutput
from app import App
import asyncio
from dotenv import load_dotenv
from agent import Agent

load_dotenv()


async def start_app():
    if gpio_available():
        input_backend = InputBackendGPIO(
            talk_bcm=config.TALK_BCM,
            mode_bcm=config.MODE_BCM,
            reset_bcm=config.RESET_BCM,
            power_bcm=config.POWER_BCM,
            indicator_red_bcm=config.LED_RED_BCM,
            indicator_green_bcm=config.LED_GREEN_BCM,
            indicator_blue_bcm=config.LED_BLUE_BCM,
        )
    else:
        print("GPIO not available, falling back to keyboard")
        input_backend = InputBackendKeyboard(
            talk_key=config.TALK_KEY,
            mode_key=config.MODE_KEY,
            reset_key=config.RESET_KEY,
            power_key=config.POWER_KEY,
        )

    audio_input = AudioInput.system_default(config.API_SAMPLE_RATE)
    audio_output = AudioOutput.system_default(config.API_SAMPLE_RATE)
    print(
        f"Using input device {audio_input.device_index} (SR {audio_input.device_sample_rate}) and output device {audio_output.device_index} (SR {audio_output.device_sample_rate})"
    )

    app = App(
        input_backend=input_backend,
        audio_input=audio_input,
        audio_output=audio_output,
        neutral_agent=Agent(
            language=config.LANGUAGE,
            agent_name=config.NEUTRAL_AGENT_NAME,
            instructions=config.NEUTRAL_INSTRUCTIONS,
            voice="cedar",
        ),
        supporter_agent=Agent(
            language=config.LANGUAGE,
            agent_name=config.SUPPORTER_AGENT_NAME,
            instructions=config.SUPPORTER_INSTRUCTIONS,
            voice="echo",
        ),
        critic_agent=Agent(
            language=config.LANGUAGE,
            agent_name=config.CRITIC_AGENT_NAME,
            instructions=config.CRITIC_INSTRUCTIONS,
            voice="shimmer",
        ),
    )

    await app.setup()
    await app.start()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.create_task(start_app())
    loop.run_forever()
