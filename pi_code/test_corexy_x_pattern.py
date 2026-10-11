"""Trace a small X-shaped relative path with a CoreXY mechanism."""

import argparse
import time

from gpiozero import OutputDevice


# BCM GPIO numbers matching the two-driver wiring plan.
MOTOR_A = {"step": 17, "direction": 27, "enable": 22}
MOTOR_B = {"step": 23, "direction": 24, "enable": 25}

FULL_STEPS_PER_REVOLUTION = 200
MICROSTEPS = 8  # MS1 and MS2 are disconnected on both drivers.
DEFAULT_RPM = 10.0
DEFAULT_CORNER_PULSES = 200
DEFAULT_PAUSE_SECONDS = 0.2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpm",
        type=float,
        default=DEFAULT_RPM,
        help="Approximate active-motor speed (default: 10 RPM)",
    )
    parser.add_argument(
        "--corner-pulses",
        type=int,
        default=DEFAULT_CORNER_PULSES,
        help="Motor pulses from center to each corner (default: 200)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help="Pause between path segments in seconds (default: 0.2)",
    )
    args = parser.parse_args()
    if args.rpm <= 0:
        parser.error("--rpm must be greater than zero")
    if args.corner_pulses <= 0:
        parser.error("--corner-pulses must be greater than zero")
    if args.pause < 0:
        parser.error("--pause cannot be negative")
    return args


def move_motor(
    label: str,
    step_pin: OutputDevice,
    direction_pin: OutputDevice,
    signed_pulses: int,
    half_period: float,
) -> None:
    print(label)
    if signed_pulses >= 0:
        direction_pin.on()
    else:
        direction_pin.off()
    time.sleep(0.05)

    for _ in range(abs(signed_pulses)):
        step_pin.on()
        time.sleep(half_period)
        step_pin.off()
        time.sleep(half_period)


def main() -> int:
    args = parse_args()
    pulses_per_second = (
        args.rpm * FULL_STEPS_PER_REVOLUTION * MICROSTEPS / 60.0
    )
    half_period = 1.0 / pulses_per_second / 2.0
    corner = args.corner_pulses

    # EN is active-low. initial_value=False starts each driver disabled.
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
            f"Tracing a small X at about {args.rpm:g} RPM using "
            f"{corner} center-to-corner pulses."
        )
        enable_a.on()
        enable_b.on()
        time.sleep(0.1)

        # For standard CoreXY kinematics, each pure diagonal is produced by
        # moving one motor while the other holds position.
        path = (
            ("Diagonal 1: center to corner", step_b, direction_b, -corner),
            ("Diagonal 1: corner to opposite corner", step_b, direction_b, 2 * corner),
            ("Returning to center", step_b, direction_b, -corner),
            ("Diagonal 2: center to corner", step_a, direction_a, -corner),
            ("Diagonal 2: corner to opposite corner", step_a, direction_a, 2 * corner),
            ("Returning to starting position", step_a, direction_a, -corner),
        )

        for label, step_pin, direction_pin, signed_pulses in path:
            move_motor(
                label,
                step_pin,
                direction_pin,
                signed_pulses,
                half_period,
            )
            time.sleep(args.pause)

        print("X pattern complete; returned to the starting position.")
    except KeyboardInterrupt:
        print("\nTest stopped. The mechanism may not be at its starting position.")
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
