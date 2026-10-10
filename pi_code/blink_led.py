"""Blink an LED connected to Raspberry Pi GPIO 2."""

from signal import pause

from gpiozero import LED


LED_GPIO = 2
SECONDS_PER_STATE = 2


def main() -> None:
    led = LED(LED_GPIO)
    led.blink(
        on_time=SECONDS_PER_STATE,
        off_time=SECONDS_PER_STATE,
        background=True,
    )
    print("Blinking GPIO 2: 2 seconds on, 2 seconds off. Press Ctrl+C to stop.")
    pause()


if __name__ == "__main__":
    main()
