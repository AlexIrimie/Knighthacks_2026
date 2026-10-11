"""Safely bench-test two TMC2209-controlled stepper motors together."""

import argparse
import time

from gpiozero import OutputDevice


# BCM GPIO numbers matching the two-driver wiring plan.
MOTOR_A = {"step": 17, "direction": 27, "enable": 22}
MOTOR_B = {"step": 23, "direction": 24, "enable": 25}

FULL_STEPS_PER_REVOLUTION = 200
MICROSTEPS = 8  # MS1 and MS2 are disconnected on both drivers.
DEFAULT_REVOLUTIONS = 0.25
DEFAULT_RPM = 100.0
DEFAULT_PAUSE_SECONDS = 1.0


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
        help="Approximate speed of both motors (default: 100 RPM)",
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


def move_together(
    step_a: OutputDevice,
    step_b: OutputDevice,
    steps: int,
    seconds_per_step: float,
) -> None:
    half_period = seconds_per_step / 2.0
    for _ in range(steps):
        step_a.on()
        step_b.on()
        time.sleep(half_period)
        step_a.off()
        step_b.off()
        time.sleep(half_period)


def main() -> int:
    args = parse_args()
    steps = round(
        args.revolutions * FULL_STEPS_PER_REVOLUTION * MICROSTEPS
    )
    pulses_per_second = (
        args.rpm * FULL_STEPS_PER_REVOLUTION * MICROSTEPS / 60.0
    )
    seconds_per_step = 1.0 / pulses_per_second

    # EN is active-low: off() drives the physical pin high and keeps the driver
    # disabled until the test explicitly calls on().
    enable_a = OutputDevice(
        MOTOR_A["enable"], active_high=False, initial_value=False
    )
    enable_b = OutputDevice(
        MOTOR_B["enable"], active_high=False, initial_value=False
    )
    direction_a = OutputDevice(MOTOR_A["direction"], initial_value=False)
    direction_b = OutputDevice(MOTOR_B["direction"], initial_value=False)
    step_a = OutputDevice(MOTOR_A["step"], initial_value=False)
    step_b = OutputDevice(MOTOR_B["step"], initial_value=False)

    try:
        print(
            f"Moving both motors {steps} pulses each direction "
            f"at about {args.rpm:g} RPM."
        )
        direction_a.off()
        direction_b.off()
        enable_a.on()
        enable_b.on()
        time.sleep(0.1)
        move_together(step_a, step_b, steps, seconds_per_step)

        print(f"Pausing for {args.pause:g} second(s)...")
        time.sleep(args.pause)

        print("Reversing both motors...")
        direction_a.on()
        direction_b.on()
        time.sleep(0.1)
        move_together(step_a, step_b, steps, seconds_per_step)
        print("Two-motor test complete.")
    except KeyboardInterrupt:
        print("\nTest stopped.")
    finally:
        step_a.off()
        step_b.off()
        enable_a.off()
        enable_b.off()
        step_a.close()
        step_b.close()
        direction_a.close()
        direction_b.close()
        enable_a.close()
        enable_b.close()
        print("Both drivers disabled.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
