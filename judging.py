"""Drive the demo hardware from a prerecorded Valorant gameplay video."""

import argparse
from collections import deque
import json
import math
from pathlib import Path
import threading
import time

try:
    import cv2
except ImportError:
    raise SystemExit(
        "Missing OpenCV. Install dependencies with: "
        "python -m pip install -r requirements.txt"
    ) from None

from player_detection import DetectionConfig, PlayerDetector

try:
    from websockets.sync.client import connect
except ImportError:
    connect = None


DEFAULT_PI_HOST = "knighthacks2026.local"
DEFAULT_PI_PORT = 8765
DEFAULT_NEAR_GAIN = 0.018
DEFAULT_FAR_GAIN = 0.055
DEFAULT_FULL_SPEED_ERROR = 220.0
DEFAULT_MAX_MOTOR_PULSES = 12
DEFAULT_ACCEL_PULSES = 3
DEFAULT_COMMAND_HZ = 10.0
DEFAULT_PIXELS_PER_PULSE = 5.0
DEFAULT_DEMO_MOTION_SCALE = 2.0
DEFAULT_BODY_SIDE_INSET = 0.10
DEFAULT_TARGET_HOLD_SECONDS = 0.20
ALIGNMENT_TOLERANCE_PX = 8
RECONNECT_SECONDS = 1.0
OPEN_TIMEOUT_SECONDS = 5.0
REPLY_TIMEOUT_SECONDS = 2.0
VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")


def find_video(requested):
    if requested:
        path = Path(requested).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Video not found: {path}")
        return path

    directories = (Path.cwd(), Path(__file__).resolve().parent)
    for directory in dict.fromkeys(directories):
        for stem in ("juding_video", "judging_video"):
            exact = directory / stem
            if exact.is_file():
                return exact
            for extension in VIDEO_EXTENSIONS:
                candidate = directory / f"{stem}{extension}"
                if candidate.is_file():
                    return candidate
    raise FileNotFoundError(
        "Add juding_video.mp4 beside judging.py, or pass --video PATH"
    )


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def adaptive_gain(near_gain, far_gain, distance, full_speed_error):
    usable_range = max(1.0, full_speed_error - ALIGNMENT_TOLERANCE_PX)
    progress = (distance - ALIGNMENT_TOLERANCE_PX) / usable_range
    return near_gain + (far_gain - near_gain) * smoothstep(progress)


def quantize_axis(error, gain, direction_sign):
    if abs(error) <= ALIGNMENT_TOLERANCE_PX:
        return 0
    pulses = int(round(error * gain * direction_sign))
    if pulses == 0:
        return 1 if error * direction_sign > 0 else -1
    return pulses


def select_body_target(result, reticle, body_side_inset):
    box = result.get("best_box")
    if box is None:
        return None

    x, y, width, height = box
    horizontal_inset = ALIGNMENT_TOLERANCE_PX + width * body_side_inset
    vertical_inset = ALIGNMENT_TOLERANCE_PX
    if width > 2 * horizontal_inset:
        left, right = x + horizontal_inset, x + width - horizontal_inset
    else:
        left = right = x + width / 2
    if height > 2 * vertical_inset:
        top, bottom = y + vertical_inset, y + height - vertical_inset
    else:
        top = bottom = y + height / 2

    return (
        min(max(reticle[0], left), right),
        min(max(reticle[1], top), bottom),
    )


def calculate_motion(target, reticle, args):
    if target is None:
        return {
            "target_visible": False,
            "on_target": False,
            "motor_a": 0,
            "motor_b": 0,
            "dx": 0.0,
            "dy": 0.0,
        }

    dx = target[0] - reticle[0]
    dy = target[1] - reticle[1]
    distance = math.hypot(dx, dy)
    if distance <= ALIGNMENT_TOLERANCE_PX:
        return {
            "target_visible": True,
            "on_target": True,
            "motor_a": 0,
            "motor_b": 0,
            "dx": dx,
            "dy": dy,
        }

    x_gain = adaptive_gain(
        args.near_x_gain,
        args.far_x_gain,
        distance,
        args.full_speed_error,
    )
    y_gain = adaptive_gain(
        args.near_y_gain,
        args.far_y_gain,
        distance,
        args.full_speed_error,
    )
    control_x, control_y = (dy, dx) if args.swap_axes else (dx, dy)
    x_pulses = quantize_axis(control_x, x_gain, args.x_sign)
    y_pulses = quantize_axis(control_y, y_gain, args.y_sign)
    x_pulses = int(round(x_pulses * args.demo_motion_scale))
    y_pulses = int(round(y_pulses * args.demo_motion_scale))

    motor_a = x_pulses + y_pulses
    motor_b = x_pulses - y_pulses
    largest = max(abs(motor_a), abs(motor_b))
    if largest > args.max_motor_pulses:
        scale = args.max_motor_pulses / largest
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


class JudgingMotionClient:
    """Send bounded commands and expose only movements acknowledged by the Pi."""

    def __init__(self, uri, motion_enabled, command_hz, accel_pulses):
        self.uri = uri
        self.motion_enabled = motion_enabled
        self.interval = 1.0 / command_hz
        self.accel_pulses = accel_pulses
        self._condition = threading.Condition()
        self._desired = {
            "target_visible": False,
            "on_target": False,
            "motor_a": 0,
            "motor_b": 0,
        }
        self._stopping = False
        self._connected = False
        self._sequence = 0
        self._acknowledged = deque(maxlen=256)
        self._strength_limit = 0
        self._last_desired_a = 0
        self._last_desired_b = 0
        self._thread = threading.Thread(
            target=self._run,
            name="judging-motion-client",
            daemon=True,
        )
        self._thread.start()

    @property
    def connected(self):
        with self._condition:
            return self._connected

    def update(self, command):
        with self._condition:
            self._desired = {
                "target_visible": bool(command["target_visible"]),
                "on_target": bool(command["on_target"]),
                "motor_a": int(command["motor_a"]),
                "motor_b": int(command["motor_b"]),
            }

    def acknowledged_after(self, after_sequence):
        with self._condition:
            return [
                dict(command)
                for command in self._acknowledged
                if command["sequence"] > after_sequence
            ]

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

    def _reset_acceleration(self):
        self._strength_limit = 0
        self._last_desired_a = 0
        self._last_desired_b = 0

    def _accelerate_pair(self, desired_a, desired_b):
        desired_strength = max(abs(desired_a), abs(desired_b))
        if desired_strength == 0:
            self._reset_acceleration()
            return 0, 0

        direction_dot = (
            self._last_desired_a * desired_a
            + self._last_desired_b * desired_b
        )
        if direction_dot < 0:
            self._strength_limit = 0

        if desired_strength <= self._strength_limit:
            self._strength_limit = desired_strength
        else:
            self._strength_limit = min(
                desired_strength,
                self._strength_limit + self.accel_pulses,
            )

        scale = self._strength_limit / desired_strength
        motor_a = int(round(desired_a * scale))
        motor_b = int(round(desired_b * scale))
        self._last_desired_a = desired_a
        self._last_desired_b = desired_b
        return motor_a, motor_b

    def _message(self, desired, stopping=False):
        self._sequence += 1
        message = dict(desired)
        message["motion_enabled"] = self.motion_enabled
        message["sequence"] = self._sequence
        safe_to_move = (
            self.motion_enabled
            and message["target_visible"]
            and not message["on_target"]
            and not stopping
        )
        if not safe_to_move:
            self._reset_acceleration()
            message["motor_a"] = 0
            message["motor_b"] = 0
            if stopping:
                message["target_visible"] = False
                message["on_target"] = False
            return message

        message["motor_a"], message["motor_b"] = self._accelerate_pair(
            message["motor_a"],
            message["motor_b"],
        )
        return message

    def _set_connected(self, connected):
        with self._condition:
            changed = connected != self._connected
            self._connected = connected
        if changed:
            print(f"Pi motion {'connected' if connected else 'disconnected'}")

    def _run(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                with connect(
                    self.uri,
                    open_timeout=OPEN_TIMEOUT_SECONDS,
                    close_timeout=1,
                    compression=None,
                ) as websocket:
                    self._set_connected(True)
                    while True:
                        cycle_started = time.perf_counter()
                        with self._condition:
                            desired = dict(self._desired)
                            stopping = self._stopping
                        message = self._message(desired, stopping)
                        websocket.send(json.dumps(message))
                        reply = json.loads(
                            websocket.recv(timeout=REPLY_TIMEOUT_SECONDS)
                        )
                        if not reply.get("ok"):
                            raise RuntimeError(
                                reply.get("error", "Pi rejected command")
                            )
                        if reply.get("sequence") != message["sequence"]:
                            raise RuntimeError("Pi returned the wrong sequence")
                        with self._condition:
                            self._acknowledged.append(dict(message))
                        if stopping:
                            return
                        remaining = self.interval - (
                            time.perf_counter() - cycle_started
                        )
                        if remaining > 0:
                            with self._condition:
                                self._condition.wait_for(
                                    lambda: self._stopping,
                                    timeout=remaining,
                                )
            except Exception as exc:
                hint = ""
                if isinstance(exc, TimeoutError):
                    hint = (
                        " Confirm aim_motion_websocket_server.py—not the "
                        "LED-only server—is running on the Pi."
                    )
                print(
                    f"Pi motion unavailable ({type(exc).__name__}: {exc}); "
                    f"retrying in {RECONNECT_SECONDS:g}s.{hint}"
                )
                self._set_connected(False)
                self._reset_acceleration()
                with self._condition:
                    if self._condition.wait_for(
                        lambda: self._stopping,
                        timeout=RECONNECT_SECONDS,
                    ):
                        return
            finally:
                self._set_connected(False)


def apply_acknowledged_motion(reticle, command, args, frame_size):
    motor_a = command["motor_a"]
    motor_b = command["motor_b"]
    control_x = (motor_a + motor_b) / 2
    control_y = (motor_a - motor_b) / 2
    screen_x = control_x * args.pixels_per_pulse * args.x_sign
    screen_y = control_y * args.pixels_per_pulse * args.y_sign
    if args.swap_axes:
        screen_x, screen_y = screen_y, screen_x

    width, height = frame_size
    return [
        min(max(reticle[0] + screen_x, 0), width - 1),
        min(max(reticle[1] + screen_y, 0), height - 1),
    ]


def draw_demo_frame(
    frame,
    result,
    reticle,
    command,
    body_mode,
    outlines,
    show_simulated_reticle,
    client,
):
    if outlines and result.get("best_box") is not None:
        x, y, width, height = result["best_box"]
        cv2.rectangle(frame, (x, y), (x + width, y + height), (0, 0, 255), 2)
        if result.get("target") is not None:
            cv2.circle(frame, result["target"], 6, (0, 255, 0), 2)

    if show_simulated_reticle:
        reticle_point = (int(round(reticle[0])), int(round(reticle[1])))
        cv2.drawMarker(
            frame,
            reticle_point,
            (255, 255, 0),
            cv2.MARKER_CROSS,
            22,
            2,
        )

    if not client.motion_enabled:
        status = "MOTION DISARMED"
        color = (30, 210, 230)
    elif not command["target_visible"]:
        status = "TARGET LOST"
        color = (40, 40, 220)
    elif command["on_target"]:
        status = "ON TARGET"
        color = (40, 210, 40)
    else:
        status = "CORRECTING"
        color = (230, 150, 20)
    connection = "PI CONNECTED" if client.connected else "PI OFFLINE"
    mode = "BODY" if body_mode else "HEAD"
    cv2.rectangle(frame, (12, 12), (620, 78), (20, 20, 20), -1)
    cv2.putText(
        frame,
        f"{status} | AIM {mode} | {connection}",
        (24, 39),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        color,
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        "5 outlines | 6 head/body | Q or Esc quit",
        (24, 66),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (230, 230, 230),
        1,
        cv2.LINE_AA,
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", help="Video path; defaults to juding_video.*")
    parser.add_argument("--pi-host", default=DEFAULT_PI_HOST)
    parser.add_argument("--pi-port", type=int, default=DEFAULT_PI_PORT)
    parser.add_argument("--enable-motion", action="store_true")
    parser.add_argument("--aim-body", action="store_true")
    parser.add_argument("--no-target-outlines", action="store_true")
    parser.add_argument("--show-simulated-reticle", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--x-sign", type=int, choices=(-1, 1), default=1)
    parser.add_argument("--y-sign", type=int, choices=(-1, 1), default=1)
    parser.add_argument("--swap-axes", action="store_true")
    parser.add_argument("--near-x-gain", type=float, default=DEFAULT_NEAR_GAIN)
    parser.add_argument("--near-y-gain", type=float, default=DEFAULT_NEAR_GAIN)
    parser.add_argument("--far-x-gain", type=float, default=DEFAULT_FAR_GAIN)
    parser.add_argument("--far-y-gain", type=float, default=DEFAULT_FAR_GAIN)
    parser.add_argument(
        "--full-speed-error",
        type=float,
        default=DEFAULT_FULL_SPEED_ERROR,
    )
    parser.add_argument(
        "--max-motor-pulses",
        type=int,
        default=DEFAULT_MAX_MOTOR_PULSES,
    )
    parser.add_argument(
        "--accel-pulses",
        type=int,
        default=DEFAULT_ACCEL_PULSES,
    )
    parser.add_argument("--command-hz", type=float, default=DEFAULT_COMMAND_HZ)
    parser.add_argument(
        "--pixels-per-pulse",
        type=float,
        default=DEFAULT_PIXELS_PER_PULSE,
        help="Simulated screen movement from one motor-axis pulse",
    )
    parser.add_argument(
        "--demo-motion-scale",
        type=float,
        default=DEFAULT_DEMO_MOTION_SCALE,
        help="Scale each judging correction before the 12-pulse safety cap",
    )
    parser.add_argument(
        "--target-hold",
        type=float,
        default=DEFAULT_TARGET_HOLD_SECONDS,
        help="Seconds to hold the last video detection through compression flicker",
    )
    parser.add_argument(
        "--body-side-inset",
        type=float,
        default=DEFAULT_BODY_SIDE_INSET,
    )
    args = parser.parse_args()

    if not 1 <= args.pi_port <= 65535:
        parser.error("--pi-port must be between 1 and 65535")
    gains = (
        args.near_x_gain,
        args.near_y_gain,
        args.far_x_gain,
        args.far_y_gain,
    )
    if any(gain <= 0 for gain in gains):
        parser.error("all gains must be greater than zero")
    if args.far_x_gain < args.near_x_gain or args.far_y_gain < args.near_y_gain:
        parser.error("far gains must be greater than or equal to near gains")
    if args.full_speed_error <= ALIGNMENT_TOLERANCE_PX:
        parser.error(
            f"--full-speed-error must exceed {ALIGNMENT_TOLERANCE_PX}"
        )
    if not 1 <= args.max_motor_pulses <= DEFAULT_MAX_MOTOR_PULSES:
        parser.error("--max-motor-pulses must be between 1 and 12")
    if not 1 <= args.accel_pulses <= args.max_motor_pulses:
        parser.error("--accel-pulses must be between 1 and max motor pulses")
    if not 1 <= args.command_hz <= 20:
        parser.error("--command-hz must be between 1 and 20")
    if args.pixels_per_pulse <= 0:
        parser.error("--pixels-per-pulse must be greater than zero")
    if args.demo_motion_scale <= 0:
        parser.error("--demo-motion-scale must be greater than zero")
    if not 0 <= args.target_hold <= 0.5:
        parser.error("--target-hold must be between 0 and 0.5 seconds")
    if not 0 <= args.body_side_inset < 0.5:
        parser.error("--body-side-inset must be at least 0 and less than 0.5")
    return args


def main():
    args = parse_args()
    if connect is None:
        print("Missing dependency. Install it with: pip install 'websockets>=15,<18'")
        return 1

    try:
        video_path = find_video(args.video)
    except FileNotFoundError as exc:
        print(exc)
        return 1

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        print(f"Could not open video: {video_path}")
        return 1
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.0

    uri = f"ws://{args.pi_host}:{args.pi_port}"
    client = JudgingMotionClient(
        uri,
        args.enable_motion,
        args.command_hz,
        args.accel_pulses,
    )
    if args.enable_motion:
        deadline = time.perf_counter() + 8
        while not client.connected and time.perf_counter() < deadline:
            time.sleep(0.05)
        if not client.connected:
            print("Could not connect to the Pi within 8 seconds; demo not started.")
            client.close()
            capture.release()
            return 1
    detector = PlayerDetector(
        DetectionConfig(
            lower_purple=(120, 45, 55),
            upper_purple=(172, 255, 255),
            min_confidence=0.56,
            lost_seconds=0.20,
            confirm_seconds=0.01,
            smooth_seconds=0.02,
        )
    )
    body_mode = args.aim_body
    outlines = not args.no_target_outlines
    reticle = None
    last_acknowledged_sequence = 0
    frame_number = 0
    last_valid_result = None
    last_valid_time = -math.inf
    playback_started = time.perf_counter()
    window_name = "KnightHacks Judging Demo"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    if args.fullscreen:
        cv2.setWindowProperty(
            window_name,
            cv2.WND_PROP_FULLSCREEN,
            cv2.WINDOW_FULLSCREEN,
        )

    print(f"Video: {video_path.resolve()} ({fps:g} FPS)")
    print(f"Motion {'ENABLED' if args.enable_motion else 'DISARMED'}: {uri}")
    print("Keys: [5] outlines, [6] head/body, [Q]/[Esc] quit.")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                client.update(calculate_motion(None, (0, 0), args))
                if not args.loop:
                    break
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                detector.reset()
                frame_number = 0
                last_valid_result = None
                last_valid_time = -math.inf
                playback_started = time.perf_counter()
                continue

            height, width = frame.shape[:2]
            frame_size = (width, height)
            if reticle is None:
                reticle = [width / 2, height / 2]
            for acknowledged in client.acknowledged_after(
                last_acknowledged_sequence
            ):
                reticle = apply_acknowledged_motion(
                    reticle,
                    acknowledged,
                    args,
                    frame_size,
                )
                last_acknowledged_sequence = acknowledged["sequence"]
            video_time = frame_number / fps
            result = detector.process(frame, video_time)
            if result.get("best_box") is not None:
                last_valid_result = result
                last_valid_time = video_time
            elif (
                last_valid_result is not None
                and video_time - last_valid_time <= args.target_hold
            ):
                held = dict(result)
                held["best_box"] = last_valid_result["best_box"]
                held["target"] = last_valid_result["target"]
                held["status"] = "holding compressed-video detection"
                result = held
            target = (
                select_body_target(result, reticle, args.body_side_inset)
                if body_mode
                else result.get("target")
            )
            command = calculate_motion(target, reticle, args)
            client.update(command)

            display = frame.copy()
            draw_demo_frame(
                display,
                result,
                reticle,
                command,
                body_mode,
                outlines,
                args.show_simulated_reticle,
                client,
            )
            cv2.imshow(window_name, display)

            frame_number += 1
            due = playback_started + frame_number / fps
            wait_ms = max(1, int((due - time.perf_counter()) * 1000))
            key = cv2.waitKey(wait_ms) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
            if key == ord("5"):
                outlines = not outlines
                print(f"Target outlines: {'ON' if outlines else 'OFF'}")
            elif key == ord("6"):
                body_mode = not body_mode
                print(f"Aim mode: {'BODY' if body_mode else 'HEAD'}")
    finally:
        client.close()
        capture.release()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
