"""Detect a head target and send tiny closed-loop CoreXY corrections to the Pi."""

import argparse
import json
import math
import sys
import threading
import time

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter

import detection_app

try:
    from websockets.sync.client import connect
except ImportError:
    connect = None


ALIGNMENT_TOLERANCE_PX = 8
DEFAULT_PI_HOST = "knighthacks2026.local"
DEFAULT_PI_PORT = 8765
DEFAULT_GAIN = 0.04
DEFAULT_MAX_MOTOR_PULSES = 12
DEFAULT_COMMAND_HZ = 10.0
RECONNECT_SECONDS = 1.0

PI_URI = f"ws://{DEFAULT_PI_HOST}:{DEFAULT_PI_PORT}"
MOTION_ENABLED = False
X_GAIN = DEFAULT_GAIN
Y_GAIN = DEFAULT_GAIN
X_SIGN = -1
Y_SIGN = -1
SWAP_AXES = False
MAX_MOTOR_PULSES = DEFAULT_MAX_MOTOR_PULSES
COMMAND_HZ = DEFAULT_COMMAND_HZ
DRAW_TARGET_OUTLINES = True


def _quantize_axis(error_px, gain, direction_sign):
    if abs(error_px) <= ALIGNMENT_TOLERANCE_PX:
        return 0
    pulses = int(round(error_px * gain * direction_sign))
    if pulses == 0:
        pulses = 1 if error_px * direction_sign > 0 else -1
    return pulses


def calculate_motion(target, screen_width, screen_height):
    """Convert screen error into a small, bounded CoreXY motor command."""
    if target is None or not screen_width or not screen_height:
        return {
            "target_visible": False,
            "on_target": False,
            "motor_a": 0,
            "motor_b": 0,
            "dx": 0.0,
            "dy": 0.0,
        }

    dx = target[0] - screen_width / 2
    dy = target[1] - screen_height / 2
    on_target = math.hypot(dx, dy) <= ALIGNMENT_TOLERANCE_PX
    if on_target:
        return {
            "target_visible": True,
            "on_target": True,
            "motor_a": 0,
            "motor_b": 0,
            "dx": dx,
            "dy": dy,
        }

    control_x, control_y = (dy, dx) if SWAP_AXES else (dx, dy)
    x_pulses = _quantize_axis(control_x, X_GAIN, X_SIGN)
    y_pulses = _quantize_axis(control_y, Y_GAIN, Y_SIGN)

    # Standard CoreXY transform. Physical direction can be corrected with the
    # x/y sign flags without rewiring a motor.
    motor_a = x_pulses + y_pulses
    motor_b = x_pulses - y_pulses
    largest = max(abs(motor_a), abs(motor_b))
    if largest > MAX_MOTOR_PULSES:
        scale = MAX_MOTOR_PULSES / largest
        motor_a = int(round(motor_a * scale))
        motor_b = int(round(motor_b * scale))

    return {
        "target_visible": True,
        "on_target": False,
        "motor_a": motor_a,
        "motor_b": motor_b,
        "dx": dx,
        "dy": dy,
    }


class PiMotionClient:
    """Send only the newest desired correction and wait for the Pi's reply."""

    def __init__(self, uri, motion_enabled, command_hz):
        self.uri = uri
        self.motion_enabled = motion_enabled
        self.interval = 1.0 / command_hz
        self._condition = threading.Condition()
        self._desired = {
            "target_visible": False,
            "on_target": False,
            "motor_a": 0,
            "motor_b": 0,
        }
        self._stopping = False
        self._connected = False
        self._last_error = None
        self._sequence = 0
        self._thread = threading.Thread(
            target=self._run,
            name="pi-motion-client",
            daemon=True,
        )
        self._thread.start()

    @property
    def connected(self):
        with self._condition:
            return self._connected

    @property
    def last_error(self):
        with self._condition:
            return self._last_error

    def update(self, command):
        with self._condition:
            self._desired = {
                "target_visible": bool(command["target_visible"]),
                "on_target": bool(command["on_target"]),
                "motor_a": int(command["motor_a"]),
                "motor_b": int(command["motor_b"]),
            }

    def close(self):
        with self._condition:
            self._desired = {
                "target_visible": False,
                "on_target": False,
                "motor_a": 0,
                "motor_b": 0,
            }
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=2)

    def _set_connection_state(self, connected, error=None):
        with self._condition:
            changed = connected != self._connected
            self._connected = connected
            self._last_error = error
        if changed:
            print(f"Pi motion {'connected' if connected else 'disconnected'}: {self.uri}")

    def _message(self, desired, stopping=False):
        self._sequence += 1
        message = dict(desired)
        message["motion_enabled"] = self.motion_enabled
        if stopping or not self.motion_enabled:
            message["motor_a"] = 0
            message["motor_b"] = 0
        if stopping:
            message["target_visible"] = False
            message["on_target"] = False
        message["sequence"] = self._sequence
        return message

    def _run(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                with connect(
                    self.uri,
                    open_timeout=2,
                    close_timeout=1,
                    compression=None,
                ) as websocket:
                    self._set_connection_state(True)
                    while True:
                        cycle_started = time.perf_counter()
                        with self._condition:
                            desired = dict(self._desired)
                            stopping = self._stopping
                        message = self._message(desired, stopping)
                        websocket.send(json.dumps(message))
                        reply = json.loads(websocket.recv(timeout=0.5))
                        if not reply.get("ok"):
                            raise RuntimeError(reply.get("error", "Pi rejected command"))
                        if stopping:
                            return
                        remaining = self.interval - (time.perf_counter() - cycle_started)
                        if remaining > 0:
                            with self._condition:
                                self._condition.wait_for(
                                    lambda: self._stopping,
                                    timeout=remaining,
                                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._set_connection_state(False, error)
                print(f"Pi motion unavailable ({error}); retrying in {RECONNECT_SECONDS:g}s.")
                with self._condition:
                    if self._condition.wait_for(
                        lambda: self._stopping,
                        timeout=RECONNECT_SECONDS,
                    ):
                        return
            finally:
                self._set_connection_state(False)


class MotionOverlay(detection_app.DetectionOverlay):
    """Existing target overlay plus bounded CoreXY command status."""

    def __init__(self, *args, **kwargs):
        self.motion_client = PiMotionClient(PI_URI, MOTION_ENABLED, COMMAND_HZ)
        self.last_command = calculate_motion(None, 0, 0)
        kwargs["draw_target_outlines"] = DRAW_TARGET_OUTLINES
        super().__init__(*args, **kwargs)
        detection_app.QApplication.instance().aboutToQuit.connect(
            self.motion_client.close
        )

    def paintEvent(self, event):
        super().paintEvent(event)

        with detection_app.state_lock:
            data = dict(detection_app.shared_state)
        if not data.get("frame_number"):
            return

        age = time.perf_counter() - data["received_at"]
        fresh = age < detection_app.CONFIG.lost_seconds
        target = data.get("target") if fresh else None
        command = calculate_motion(
            target,
            data.get("screen_width"),
            data.get("screen_height"),
        )
        self.last_command = command
        self.motion_client.update(command)

        if not MOTION_ENABLED:
            status = "MOTION DISARMED"
            background = QColor(100, 80, 10, 220)
        elif not command["target_visible"]:
            status = "TARGET LOST"
            background = QColor(120, 30, 30, 220)
        elif command["on_target"]:
            status = "ON TARGET"
            background = QColor(0, 120, 40, 220)
        else:
            status = "CORRECTING"
            background = QColor(20, 70, 150, 220)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(QFont("Consolas", 16, QFont.Weight.Bold))
        box = QRectF((self.width() - 390) / 2, 20, 390, 72)
        painter.fillRect(box, background)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            QRectF(box.left(), box.top() + 5, box.width(), 28),
            Qt.AlignmentFlag.AlignCenter,
            status,
        )
        painter.setFont(QFont("Consolas", 10))
        details = (
            f"error ({command['dx']:+.0f}, {command['dy']:+.0f}) px   "
            f"motors A {command['motor_a']:+d}, B {command['motor_b']:+d}   "
            f"{'PI CONNECTED' if self.motion_client.connected else 'PI OFFLINE'}"
        )
        painter.drawText(
            QRectF(box.left(), box.top() + 37, box.width(), 26),
            Qt.AlignmentFlag.AlignCenter,
            details,
        )
        painter.end()


def main():
    global PI_URI, MOTION_ENABLED, X_GAIN, Y_GAIN, X_SIGN, Y_SIGN
    global SWAP_AXES, MAX_MOTOR_PULSES, COMMAND_HZ, DRAW_TARGET_OUTLINES

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pi-host", default=DEFAULT_PI_HOST)
    parser.add_argument("--pi-port", type=int, default=DEFAULT_PI_PORT)
    parser.add_argument(
        "--enable-motion",
        action="store_true",
        help="Actually send nonzero motor commands; omitted means overlay/LED only",
    )
    parser.add_argument("--x-gain", type=float, default=DEFAULT_GAIN)
    parser.add_argument("--y-gain", type=float, default=DEFAULT_GAIN)
    parser.add_argument("--x-sign", type=int, choices=(-1, 1), default=-1)
    parser.add_argument("--y-sign", type=int, choices=(-1, 1), default=-1)
    parser.add_argument(
        "--swap-axes",
        action="store_true",
        help="Map screen X to platform Y and screen Y to platform X",
    )
    parser.add_argument(
        "--max-motor-pulses",
        type=int,
        default=DEFAULT_MAX_MOTOR_PULSES,
    )
    parser.add_argument("--command-hz", type=float, default=DEFAULT_COMMAND_HZ)
    parser.add_argument(
        "--no-target-outlines",
        action="store_true",
        help="Hide the red body box and green head circle",
    )
    args, remaining = parser.parse_known_args()

    if not 1 <= args.pi_port <= 65535:
        parser.error("--pi-port must be between 1 and 65535")
    if args.x_gain <= 0 or args.y_gain <= 0:
        parser.error("--x-gain and --y-gain must be greater than zero")
    if not 1 <= args.max_motor_pulses <= 12:
        parser.error("--max-motor-pulses must be between 1 and 12")
    if not 1 <= args.command_hz <= 20:
        parser.error("--command-hz must be between 1 and 20")
    if connect is None:
        print("Missing dependency. Install it with: pip install 'websockets>=15,<18'")
        return 1

    PI_URI = f"ws://{args.pi_host}:{args.pi_port}"
    MOTION_ENABLED = args.enable_motion
    X_GAIN = args.x_gain
    Y_GAIN = args.y_gain
    X_SIGN = args.x_sign
    Y_SIGN = args.y_sign
    SWAP_AXES = args.swap_axes
    MAX_MOTOR_PULSES = args.max_motor_pulses
    COMMAND_HZ = args.command_hz
    DRAW_TARGET_OUTLINES = not args.no_target_outlines
    sys.argv = [sys.argv[0], *remaining]

    print(
        f"Motion {'ENABLED' if MOTION_ENABLED else 'DISARMED'}: "
        f"gain=({X_GAIN:g}, {Y_GAIN:g}), signs=({X_SIGN:+d}, {Y_SIGN:+d}), "
        f"max={MAX_MOTOR_PULSES} pulses, rate={COMMAND_HZ:g} Hz."
    )
    detection_app.DetectionOverlay = MotionOverlay
    return detection_app.main()


if __name__ == "__main__":
    raise SystemExit(main())
