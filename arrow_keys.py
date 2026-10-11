"""Move the CoreXY mouse pad manually with global arrow keys."""

import argparse
import ctypes
import json
import sys
import threading
import time

try:
    from websockets.sync.client import connect
except ImportError:
    connect = None


DEFAULT_PI_HOST = "knighthacks2026.local"
DEFAULT_PI_PORT = 8765
DEFAULT_PULSES_PER_COMMAND = 6
DEFAULT_COMMAND_HZ = 20.0
MAX_MOTOR_PULSES = 12
RECONNECT_SECONDS = 1.0
OPEN_TIMEOUT_SECONDS = 5.0
REPLY_TIMEOUT_SECONDS = 2.0

WINDOWS_VIRTUAL_KEYS = {
    "escape": 0x1B,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}


class WindowsKeyboard:
    def __init__(self):
        self.user32 = ctypes.windll.user32

    def is_pressed(self, name):
        return bool(
            self.user32.GetAsyncKeyState(WINDOWS_VIRTUAL_KEYS[name]) & 0x8000
        )

    def close(self):
        pass


class ListenerKeyboard:
    """Global keyboard state via pynput on macOS and other non-Windows hosts."""

    def __init__(self):
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise RuntimeError(
                "Missing pynput. Install with: python -m pip install pynput"
            ) from exc

        self._lock = threading.Lock()
        self._pressed = set()
        self._names = {
            keyboard.Key.esc: "escape",
            keyboard.Key.left: "left",
            keyboard.Key.up: "up",
            keyboard.Key.right: "right",
            keyboard.Key.down: "down",
        }

        def on_press(key):
            name = self._names.get(key)
            if name is not None:
                with self._lock:
                    self._pressed.add(name)

        def on_release(key):
            name = self._names.get(key)
            if name is not None:
                with self._lock:
                    self._pressed.discard(name)

        self._listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release,
        )
        self._listener.start()
        self._listener.wait()

    def is_pressed(self, name):
        with self._lock:
            return name in self._pressed

    def close(self):
        self._listener.stop()
        self._listener.join(timeout=1)


def create_keyboard():
    if sys.platform == "win32":
        return WindowsKeyboard()
    return ListenerKeyboard()


def read_direction(keyboard):
    horizontal = int(keyboard.is_pressed("right")) - int(
        keyboard.is_pressed("left")
    )
    vertical = int(keyboard.is_pressed("down")) - int(
        keyboard.is_pressed("up")
    )
    return horizontal, vertical


def corexy_command(horizontal, vertical, args):
    if args.swap_axes:
        horizontal, vertical = vertical, horizontal
    axis_x = horizontal * args.pulses_per_command * args.x_sign
    axis_y = vertical * args.pulses_per_command * args.y_sign
    motor_a = axis_x + axis_y
    motor_b = axis_x - axis_y

    largest = max(abs(motor_a), abs(motor_b))
    if largest > MAX_MOTOR_PULSES:
        scale = MAX_MOTOR_PULSES / largest
        motor_a = int(round(motor_a * scale))
        motor_b = int(round(motor_b * scale))
    return motor_a, motor_b


def direction_name(horizontal, vertical):
    names = []
    if vertical < 0:
        names.append("UP")
    elif vertical > 0:
        names.append("DOWN")
    if horizontal < 0:
        names.append("LEFT")
    elif horizontal > 0:
        names.append("RIGHT")
    return "+".join(names) if names else "STOPPED"


def message(sequence, motor_a=0, motor_b=0, motion_enabled=True):
    moving = bool(motor_a or motor_b) and motion_enabled
    return {
        "target_visible": moving,
        "on_target": False,
        "motion_enabled": motion_enabled,
        "motor_a": motor_a if moving else 0,
        "motor_b": motor_b if moving else 0,
        "sequence": sequence,
    }


def exchange(websocket, command):
    websocket.send(json.dumps(command))
    reply = json.loads(websocket.recv(timeout=REPLY_TIMEOUT_SECONDS))
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "Pi rejected command"))
    if reply.get("sequence") != command["sequence"]:
        raise RuntimeError("Pi returned the wrong sequence")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi-host", default=DEFAULT_PI_HOST)
    parser.add_argument("--pi-port", type=int, default=DEFAULT_PI_PORT)
    parser.add_argument(
        "--enable-motion",
        action="store_true",
        help="Required to send nonzero motor commands",
    )
    parser.add_argument(
        "--pulses-per-command",
        type=int,
        default=DEFAULT_PULSES_PER_COMMAND,
    )
    parser.add_argument("--command-hz", type=float, default=DEFAULT_COMMAND_HZ)
    parser.add_argument(
        "--x-sign",
        type=int,
        choices=(-1, 1),
        default=-1,
        help="Reverse with 1 if physical left/right is backward",
    )
    parser.add_argument(
        "--y-sign",
        type=int,
        choices=(-1, 1),
        default=-1,
        help="Reverse with 1 if physical up/down is backward",
    )
    parser.add_argument("--swap-axes", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.pi_port <= 65535:
        parser.error("--pi-port must be between 1 and 65535")
    if not 1 <= args.pulses_per_command <= MAX_MOTOR_PULSES:
        parser.error("--pulses-per-command must be between 1 and 12")
    if not 1 <= args.command_hz <= 20:
        parser.error("--command-hz must be between 1 and 20")
    return args


def main():
    args = parse_args()
    if connect is None:
        print(
            "Missing dependency. Install it with: "
            "python -m pip install 'websockets>=15,<18'"
        )
        return 1
    try:
        keyboard = create_keyboard()
    except (AttributeError, RuntimeError) as exc:
        print(exc)
        return 1

    uri = f"ws://{args.pi_host}:{args.pi_port}"
    interval = 1.0 / args.command_hz
    sequence = 0
    running = True
    last_direction = None
    print(f"Motion {'ENABLED' if args.enable_motion else 'DISARMED'}: {uri}")
    print("Hold arrow keys to move; combine two for diagonals; Esc or Ctrl+C quits.")
    if not args.enable_motion:
        print("Preview only. Restart with --enable-motion to move the pad.")

    try:
        while running:
            try:
                with connect(
                    uri,
                    open_timeout=OPEN_TIMEOUT_SECONDS,
                    close_timeout=1,
                    compression=None,
                ) as websocket:
                    print("Pi motion connected.")
                    while running:
                        cycle_started = time.perf_counter()
                        if keyboard.is_pressed("escape"):
                            running = False
                            break

                        horizontal, vertical = read_direction(keyboard)
                        motor_a, motor_b = corexy_command(
                            horizontal,
                            vertical,
                            args,
                        )
                        sequence += 1
                        command = message(
                            sequence,
                            motor_a,
                            motor_b,
                            args.enable_motion,
                        )
                        exchange(websocket, command)

                        current_direction = direction_name(horizontal, vertical)
                        if current_direction != last_direction:
                            shown = (
                                current_direction
                                if args.enable_motion
                                else f"{current_direction} (DISARMED)"
                            )
                            print(
                                f"{shown}: motors "
                                f"A {command['motor_a']:+d}, "
                                f"B {command['motor_b']:+d}"
                            )
                            last_direction = current_direction

                        remaining = interval - (
                            time.perf_counter() - cycle_started
                        )
                        if remaining > 0:
                            time.sleep(remaining)

                    sequence += 1
                    try:
                        exchange(
                            websocket,
                            message(sequence, motion_enabled=args.enable_motion),
                        )
                    except Exception:
                        pass
            except Exception as exc:
                if not running:
                    break
                print(
                    f"Pi motion unavailable ({type(exc).__name__}: {exc}); "
                    f"retrying in {RECONNECT_SECONDS:g}s."
                )
                time.sleep(RECONNECT_SECONDS)
    except KeyboardInterrupt:
        print("Stopping arrow-key control.")
    finally:
        keyboard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
