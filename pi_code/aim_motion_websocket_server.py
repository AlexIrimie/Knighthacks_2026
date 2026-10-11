"""Receive bounded aim corrections and drive two CoreXY TMC2209 modules."""

import argparse
import asyncio
import json
import time

from gpiozero import LED, OutputDevice
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


DEFAULT_PORT = 8765
DEFAULT_LED_GPIO = 2
DEFAULT_RPM = 100.0
DEFAULT_MAX_PULSES = 12
COMMAND_TIMEOUT_SECONDS = 0.3
FULL_STEPS_PER_REVOLUTION = 200
MICROSTEPS = 8

MOTOR_A = {"step": 17, "direction": 27, "enable": 22}
MOTOR_B = {"step": 23, "direction": 24, "enable": 25}


def parse_command(message, max_pulses):
    try:
        command = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None, "invalid JSON"
    if not isinstance(command, dict):
        return None, "command must be an object"

    for name in ("target_visible", "on_target", "motion_enabled"):
        if type(command.get(name)) is not bool:
            return None, f"{name} must be a boolean"
    for name in ("motor_a", "motor_b", "sequence"):
        if type(command.get(name)) is not int:
            return None, f"{name} must be an integer"
    if abs(command["motor_a"]) > max_pulses or abs(command["motor_b"]) > max_pulses:
        return None, f"motor command exceeds {max_pulses}-pulse limit"
    if (
        command["on_target"]
        or not command["target_visible"]
        or not command["motion_enabled"]
    ) and (
        command["motor_a"] or command["motor_b"]
    ):
        return None, "movement is forbidden without an off-center visible target"
    return command, None


class AimHardware:
    def __init__(self, led_gpio, rpm):
        pulses_per_second = rpm * FULL_STEPS_PER_REVOLUTION * MICROSTEPS / 60.0
        self.half_period = 1.0 / pulses_per_second / 2.0
        self.led = LED(led_gpio)
        self.enable_a = OutputDevice(
            MOTOR_A["enable"], active_high=False, initial_value=False
        )
        self.enable_b = OutputDevice(
            MOTOR_B["enable"], active_high=False, initial_value=False
        )
        self.direction_a = OutputDevice(MOTOR_A["direction"], initial_value=False)
        self.direction_b = OutputDevice(MOTOR_B["direction"], initial_value=False)
        self.step_a = OutputDevice(MOTOR_A["step"], initial_value=False)
        self.step_b = OutputDevice(MOTOR_B["step"], initial_value=False)
        self.enabled = False

    def enable(self):
        if not self.enabled:
            self.enable_a.on()
            self.enable_b.on()
            self.enabled = True
            time.sleep(0.001)

    def disable(self):
        self.step_a.off()
        self.step_b.off()
        self.enable_a.off()
        self.enable_b.off()
        self.enabled = False

    def set_led(self, on_target):
        self.led.on() if on_target else self.led.off()

    def move(self, motor_a, motor_b):
        total = max(abs(motor_a), abs(motor_b))
        if total == 0:
            return

        self.enable()
        self.direction_a.on() if motor_a >= 0 else self.direction_a.off()
        self.direction_b.on() if motor_b >= 0 else self.direction_b.off()
        time.sleep(0.0001)

        count_a = abs(motor_a)
        count_b = abs(motor_b)
        accumulator_a = 0
        accumulator_b = 0
        for _ in range(total):
            accumulator_a += count_a
            accumulator_b += count_b
            pulse_a = accumulator_a >= total
            pulse_b = accumulator_b >= total
            if pulse_a:
                accumulator_a -= total
                self.step_a.on()
            if pulse_b:
                accumulator_b -= total
                self.step_b.on()
            time.sleep(self.half_period)
            if pulse_a:
                self.step_a.off()
            if pulse_b:
                self.step_b.off()
            time.sleep(self.half_period)

    def safe_stop(self):
        self.set_led(False)
        self.disable()

    def close(self):
        self.safe_stop()
        for device in (
            self.step_a,
            self.step_b,
            self.direction_a,
            self.direction_b,
            self.enable_a,
            self.enable_b,
            self.led,
        ):
            device.close()


async def handle_connection(websocket, hardware, max_pulses):
    address = websocket.remote_address
    print(f"PC connected: {address}")
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                hardware.safe_stop()
                continue

            command, error = parse_command(message, max_pulses)
            if error:
                hardware.safe_stop()
                await websocket.send(json.dumps({"ok": False, "error": error}))
                continue

            if not command["motion_enabled"]:
                hardware.set_led(command["on_target"])
                hardware.disable()
            elif not command["target_visible"]:
                hardware.safe_stop()
            else:
                hardware.set_led(command["on_target"])
                hardware.enable()
                if not command["on_target"]:
                    hardware.move(command["motor_a"], command["motor_b"])

            await websocket.send(
                json.dumps({"ok": True, "sequence": command["sequence"]})
            )
    except ConnectionClosed:
        pass
    finally:
        hardware.safe_stop()
        print(f"PC disconnected: {address}")


async def run_server(host, port, hardware, max_pulses):
    async with serve(
        lambda websocket: handle_connection(websocket, hardware, max_pulses),
        host,
        port,
        compression=None,
    ):
        print(f"Aim/motion WebSocket server listening on ws://{host}:{port}")
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--led-gpio", type=int, default=DEFAULT_LED_GPIO)
    parser.add_argument("--rpm", type=float, default=DEFAULT_RPM)
    parser.add_argument("--max-pulses", type=int, default=DEFAULT_MAX_PULSES)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 1 <= args.rpm <= 400:
        parser.error("--rpm must be between 1 and 400")
    if not 1 <= args.max_pulses <= DEFAULT_MAX_PULSES:
        parser.error(f"--max-pulses must be between 1 and {DEFAULT_MAX_PULSES}")

    hardware = AimHardware(args.led_gpio, args.rpm)
    try:
        asyncio.run(run_server(args.host, args.port, hardware, args.max_pulses))
    except KeyboardInterrupt:
        print("Stopping aim/motion server.")
    finally:
        hardware.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
