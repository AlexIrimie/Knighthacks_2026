"""Run two CoreXY motors in opposite directions briefly, then stop."""

import argparse
import time

from gpiozero import OutputDevice


# BCM GPIO numbers matching the two-driver wiring plan.
MOTOR_A = {"step": 17, "direction": 27, "enable": 22}
MOTOR_B = {"step": 23, "direction": 24, "enable": 25}

FULL_STEPS_PER_REVOLUTION = 200
MICROSTEPS = 8  # MS1 and MS2 are disconnected on both drivers.
DEFAULT_RPM = 10.0
DEFAULT_DURATION_SECONDS = 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpm",
        type=float,
        default=DEFAULT_RPM,
        help="Approximate speed of both motors (default: 10 RPM)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
        help="Movement duration in seconds (default: 1)",
    )
    args = parser.parse_args()
    if args.rpm <= 0:
        parser.error("--rpm must be greater than zero")
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    return args


def main() -> int:
    args = parse_args()
    pulses_per_second = (
        args.rpm * FULL_STEPS_PER_REVOLUTION * MICROSTEPS / 60.0
    )
    pulse_count = max(1, round(pulses_per_second * args.duration))
    half_period = 1.0 / pulses_per_second / 2.0

    # EN is active-low. initial_value=False starts each driver disabled.
    enable_a = OutputDevice(
        MOTOR_A["enable"], active_high=False, initial_value=False
    )
    enable_b = OutputDevice(
        MOTOR_B["enable"], active_high=False, initial_value=False
    )
    direction_a = OutputDevice(MOTOR_A["direction"], initial_value=False)
    direction_b = OutputDevice(MOTOR_B["direction"], initial_value=True)
    step_a = OutputDevice(MOTOR_A["step"], initial_value=False)
    step_b = OutputDevice(MOTOR_B["step"], initial_value=False)

    try:
        print(
            f"Running motors in opposite directions at about {args.rpm:g} RPM "
            f"for {args.duration:g} second(s)."
        )
        direction_a.off()
        direction_b.on()
        enable_a.on()
        enable_b.on()
        time.sleep(0.1)

        for _ in range(pulse_count):
            step_a.on()
            step_b.on()
            time.sleep(half_period)
            step_a.off()
            step_b.off()
            time.sleep(half_period)

        print("Movement complete.")
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
        print("Both drivers disabled. No reverse movement was performed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
