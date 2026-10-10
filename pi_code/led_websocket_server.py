"""Control a Raspberry Pi LED from on-target WebSocket messages."""

import argparse
import asyncio
import json

from gpiozero import LED
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


DEFAULT_GPIO = 2
DEFAULT_PORT = 8765
COMMAND_TIMEOUT_SECONDS = 1.0


class LEDController:
    def __init__(self, gpio_pin):
        self.led = LED(gpio_pin)
        self.on_target = False

    def set(self, on_target):
        on_target = bool(on_target)
        if on_target == self.on_target:
            return
        self.on_target = on_target
        self.led.on() if on_target else self.led.off()
        print("LED ON" if on_target else "LED OFF")

    def close(self):
        self.set(False)
        self.led.close()


def parse_command(message):
    """Accept only JSON objects containing a real boolean on_target value."""
    try:
        command = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(command, dict) or type(command.get("on_target")) is not bool:
        return None
    return command["on_target"]


async def handle_connection(websocket, controller):
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
                controller.set(False)
                continue
            on_target = parse_command(message)
            if on_target is None:
                print(f"Ignored invalid command from {address}")
                controller.set(False)
                continue
            controller.set(on_target)
    except ConnectionClosed:
        pass
    finally:
        controller.set(False)
        print(f"PC disconnected: {address}")


async def run_server(host, port, controller):
    async with serve(
        lambda websocket: handle_connection(websocket, controller),
        host,
        port,
        compression=None,
    ):
        print(f"LED WebSocket server listening on ws://{host}:{port}")
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Listen address (default: all IPv4 interfaces)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--gpio", type=int, default=DEFAULT_GPIO, help="BCM GPIO number")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    controller = LEDController(args.gpio)
    try:
        asyncio.run(run_server(args.host, args.port, controller))
    except KeyboardInterrupt:
        print("Stopping LED server.")
    finally:
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
