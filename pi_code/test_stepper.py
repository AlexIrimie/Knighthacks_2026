"""Safely bench-test one TMC2209-controlled stepper motor."""

import argparse
import time

from gpiozero import OutputDevice


# BCM GPIO numbers. These match the first-driver wiring plan.
STEP_GPIO = 17  # Physical pin 11
DIR_GPIO = 27  # Physical pin 13
ENABLE_GPIO = 22  # Physical pin 15; TMC2209 EN is active-low

FULL_STEPS_PER_REVOLUTION = 200
MICROSTEPS = 8  # MS1 and MS2 left disconnected use internal pull-downs.
DEFAULT_REVOLUTIONS = 0.25
DEFAULT_RPM = 5.0
DEFAULT_PAUSE_SECONDS = 1.0


def pulse_count(revolutions: float) -> int:
    return round(revolutions * FULL_STEPS_PER_REVOLUTION * MICROSTEPS)


def step_delay(rpm: float) -> float:
    pulses_per_second = rpm * FULL_STEPS_PER_REVOLUTION * MICROSTEPS / 60.0
    return 1.0 / pulses_per_second


def move(step_pin: OutputDevice, steps: int, seconds_per_step: float) -> None:
    half_period = seconds_per_step / 2.0
    for _ in range(steps):
        step_pin.on()
        time.sleep(half_period)
        step_pin.off()
        time.sleep(half_period)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--revolutions",
        type=float,
        default=DEFAULT_REVOLUTIONS,
        help="Revolutions in each direction (default: 0.25)",
    )
    parser.add_argument(
        "--rpm",
        type=float,
        default=DEFAULT_RPM,
        help="Approximate motor speed (default: 5 RPM)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help="Seconds to pause before reversing (default: 1)",
    )
    args = parser.parse_args()
    if args.revolutions <= 0:
        parser.error("--revolutions must be greater than zero")
    if args.rpm <= 0:
        parser.error("--rpm must be greater than zero")
    if args.pause < 0:
        parser.error("--pause cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    steps = pulse_count(args.revolutions)
    seconds_per_step = step_delay(args.rpm)

    # active_high=False makes off() drive EN high (disabled) and on() drive it
    # low (enabled). Starting disabled prevents an unexpected energized motor.
    enable_pin = OutputDevice(ENABLE_GPIO, active_high=False, initial_value=False)
    direction_pin = OutputDevice(DIR_GPIO, initial_value=False)
    step_pin = OutputDevice(STEP_GPIO, initial_value=False)

    try:
        print(
            f"Testing GPIO STEP={STEP_GPIO}, DIR={DIR_GPIO}, EN={ENABLE_GPIO}; "
            f"{steps} pulses each direction at about {args.rpm:g} RPM."
        )
        print("Enabling driver and moving forward...")
        enable_pin.on()
        direction_pin.off()
        time.sleep(0.1)
        move(step_pin, steps, seconds_per_step)

        print(f"Pausing for {args.pause:g} second(s)...")
        time.sleep(args.pause)

        print("Moving backward...")
        direction_pin.on()
        time.sleep(0.1)
        move(step_pin, steps, seconds_per_step)
        print("Test complete.")
    except KeyboardInterrupt:
        print("\nTest stopped.")
    finally:
        step_pin.off()
        enable_pin.off()
        step_pin.close()
        direction_pin.close()
        enable_pin.close()
        print("Driver disabled.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
