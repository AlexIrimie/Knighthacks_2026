"""Experimental adaptive CoreXY aim controller with command acceleration."""

import argparse
import ctypes
import math
import sys

import valorant_motion_overlay as motion


DEFAULT_NEAR_GAIN = 0.018
DEFAULT_FAR_GAIN = 0.055
DEFAULT_FULL_SPEED_ERROR_PX = 220.0
DEFAULT_ACCEL_PULSES = 3
DEFAULT_BODY_SIDE_INSET = 0.10

NEAR_X_GAIN = DEFAULT_NEAR_GAIN
NEAR_Y_GAIN = DEFAULT_NEAR_GAIN
FAR_X_GAIN = DEFAULT_FAR_GAIN
FAR_Y_GAIN = DEFAULT_FAR_GAIN
FULL_SPEED_ERROR_PX = DEFAULT_FULL_SPEED_ERROR_PX
ACCEL_PULSES = DEFAULT_ACCEL_PULSES
BODY_SIDE_INSET = DEFAULT_BODY_SIDE_INSET
AIM_BODY = False


def select_runtime_target(data, fresh):
    """Select the head or body target using the current hotkey mode."""
    if AIM_BODY:
        return select_body_target(data, fresh)
    return data.get("target") if fresh else None


def select_body_target(data, fresh):
    """Return the shortest target point that puts the reticle in the body box."""
    if not fresh:
        return None
    box = data.get("best_box")
    screen_width = data.get("screen_width")
    screen_height = data.get("screen_height")
    if box is None or not screen_width or not screen_height:
        return None

    x, y, width, height = box
    horizontal_inset = (
        motion.ALIGNMENT_TOLERANCE_PX + width * BODY_SIDE_INSET
    )
    vertical_inset = motion.ALIGNMENT_TOLERANCE_PX
    if width > 2 * horizontal_inset:
        left, right = x + horizontal_inset, x + width - horizontal_inset
    else:
        left = right = x + width / 2
    if height > 2 * vertical_inset:
        top, bottom = y + vertical_inset, y + height - vertical_inset
    else:
        top = bottom = y + height / 2

    reticle_x = screen_width / 2
    reticle_y = screen_height / 2
    return (
        min(max(reticle_x, left), right),
        min(max(reticle_y, top), bottom),
    )


def _smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _adaptive_gain(near_gain, far_gain, distance):
    usable_range = max(
        1.0,
        FULL_SPEED_ERROR_PX - motion.ALIGNMENT_TOLERANCE_PX,
    )
    progress = (distance - motion.ALIGNMENT_TOLERANCE_PX) / usable_range
    blend = _smoothstep(progress)
    return near_gain + (far_gain - near_gain) * blend


def calculate_adaptive_motion(target, screen_width, screen_height):
    """Use low gain near the reticle and smoothly increase it farther away."""
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
    distance = math.hypot(dx, dy)
    if distance <= motion.ALIGNMENT_TOLERANCE_PX:
        return {
            "target_visible": True,
            "on_target": True,
            "motor_a": 0,
            "motor_b": 0,
            "dx": dx,
            "dy": dy,
        }

    x_gain = _adaptive_gain(NEAR_X_GAIN, FAR_X_GAIN, distance)
    y_gain = _adaptive_gain(NEAR_Y_GAIN, FAR_Y_GAIN, distance)
    control_x, control_y = (dy, dx) if motion.SWAP_AXES else (dx, dy)
    x_pulses = motion._quantize_axis(control_x, x_gain, motion.X_SIGN)
    y_pulses = motion._quantize_axis(control_y, y_gain, motion.Y_SIGN)

    motor_a = x_pulses + y_pulses
    motor_b = x_pulses - y_pulses
    largest = max(abs(motor_a), abs(motor_b))
    if largest > motion.MAX_MOTOR_PULSES:
        scale = motion.MAX_MOTOR_PULSES / largest
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


BasePiMotionClient = motion.PiMotionClient


class AdaptivePiMotionClient(BasePiMotionClient):
    """Ramp up pulse count without delaying stops or continuing a stale direction."""

    def __init__(self, *args, **kwargs):
        self._strength_limit = 0
        self._last_desired_a = 0
        self._last_desired_b = 0
        super().__init__(*args, **kwargs)

    def _accelerate_pair(self, desired_a, desired_b):
        desired_strength = max(abs(desired_a), abs(desired_b))
        if desired_strength == 0:
            self._strength_limit = 0
            self._last_desired_a = 0
            self._last_desired_b = 0
            return 0, 0

        direction_dot = (
            self._last_desired_a * desired_a
            + self._last_desired_b * desired_b
        )
        if direction_dot < 0:
            # A substantial reversal begins gently in the new direction.
            self._strength_limit = 0

        if desired_strength <= self._strength_limit:
            self._strength_limit = desired_strength
        else:
            self._strength_limit = min(
                desired_strength,
                self._strength_limit + ACCEL_PULSES,
            )

        scale = self._strength_limit / desired_strength
        motor_a = int(round(desired_a * scale))
        motor_b = int(round(desired_b * scale))
        self._last_desired_a = desired_a
        self._last_desired_b = desired_b
        return motor_a, motor_b

    def _message(self, desired, stopping=False):
        message = super()._message(desired, stopping)
        safe_to_move = (
            message["motion_enabled"]
            and message["target_visible"]
            and not message["on_target"]
            and not stopping
        )
        if not safe_to_move:
            self._strength_limit = 0
            self._last_desired_a = 0
            self._last_desired_b = 0
            message["motor_a"] = 0
            message["motor_b"] = 0
            return message

        message["motor_a"], message["motor_b"] = self._accelerate_pair(
            message["motor_a"],
            message["motor_b"],
        )
        return message


BaseMotionOverlay = motion.MotionOverlay


class AdaptiveMotionOverlay(BaseMotionOverlay):
    """Add global Windows hotkeys for outlines and head/body aim mode."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hotkey_down = {"outlines": False, "aim": False}
        self._user32 = None
        try:
            self._user32 = ctypes.windll.user32
        except AttributeError:
            print("Adaptive hotkeys unavailable: Windows input API not found.")
            return

        self._hotkey_timer = motion.detection_app.QTimer(self)
        self._hotkey_timer.timeout.connect(self._poll_hotkeys)
        self._hotkey_timer.start(30)
        print("Hotkeys: [5] toggle target outlines; [6] toggle head/body aim.")

    def _pressed(self, *virtual_keys):
        return any(
            self._user32.GetAsyncKeyState(key) & 0x8000
            for key in virtual_keys
        )

    def _poll_hotkeys(self):
        global AIM_BODY

        # Support both the number row and numeric keypad.
        states = {
            "outlines": self._pressed(0x35, 0x65),
            "aim": self._pressed(0x36, 0x66),
        }
        for name, pressed in states.items():
            if pressed and not self._hotkey_down[name]:
                if name == "outlines":
                    self.draw_target_outlines = not self.draw_target_outlines
                    state = "ON" if self.draw_target_outlines else "OFF"
                    print(f"Target outlines: {state}")
                else:
                    AIM_BODY = not AIM_BODY
                    print(f"Aim mode: {'BODY' if AIM_BODY else 'HEAD'}")
                self.update()
            self._hotkey_down[name] = pressed

    def paintEvent(self, event):
        super().paintEvent(event)

        painter = motion.QPainter(self)
        painter.setFont(motion.QFont("Consolas", 9, motion.QFont.Weight.Bold))
        box = motion.QRectF((self.width() - 390) / 2, 96, 390, 22)
        painter.fillRect(box, motion.QColor(15, 15, 15, 190))
        painter.setPen(motion.QColor(255, 255, 255))
        painter.drawText(
            box,
            motion.Qt.AlignmentFlag.AlignCenter,
            f"[5] OUTLINES {'ON' if self.draw_target_outlines else 'OFF'}    "
            f"[6] AIM {'BODY' if AIM_BODY else 'HEAD'}",
        )
        painter.end()


def main():
    global NEAR_X_GAIN, NEAR_Y_GAIN, FAR_X_GAIN, FAR_Y_GAIN
    global FULL_SPEED_ERROR_PX, ACCEL_PULSES, BODY_SIDE_INSET, AIM_BODY

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--near-x-gain", type=float, default=DEFAULT_NEAR_GAIN)
    parser.add_argument("--near-y-gain", type=float, default=DEFAULT_NEAR_GAIN)
    parser.add_argument("--far-x-gain", type=float, default=DEFAULT_FAR_GAIN)
    parser.add_argument("--far-y-gain", type=float, default=DEFAULT_FAR_GAIN)
    parser.add_argument(
        "--full-speed-error",
        type=float,
        default=DEFAULT_FULL_SPEED_ERROR_PX,
        help="Pixel distance where the far gains are fully applied",
    )
    parser.add_argument(
        "--accel-pulses",
        type=int,
        default=DEFAULT_ACCEL_PULSES,
        help="Maximum pulse-count increase per command",
    )
    parser.add_argument(
        "--aim-body",
        action="store_true",
        help="Stop when the reticle enters the detected body box",
    )
    parser.add_argument(
        "--body-side-inset",
        type=float,
        default=DEFAULT_BODY_SIDE_INSET,
        help="Fraction trimmed from each side of the body box (default 0.10)",
    )
    args, remaining = parser.parse_known_args()

    gains = (
        args.near_x_gain,
        args.near_y_gain,
        args.far_x_gain,
        args.far_y_gain,
    )
    if any(gain <= 0 for gain in gains):
        parser.error("adaptive gains must be greater than zero")
    if args.far_x_gain < args.near_x_gain or args.far_y_gain < args.near_y_gain:
        parser.error("far gains must be greater than or equal to near gains")
    if args.full_speed_error <= motion.ALIGNMENT_TOLERANCE_PX:
        parser.error(
            f"--full-speed-error must be greater than "
            f"{motion.ALIGNMENT_TOLERANCE_PX} pixels"
        )
    if not 1 <= args.accel_pulses <= motion.DEFAULT_MAX_MOTOR_PULSES:
        parser.error(
            f"--accel-pulses must be between 1 and "
            f"{motion.DEFAULT_MAX_MOTOR_PULSES}"
        )
    if not 0 <= args.body_side_inset < 0.5:
        parser.error("--body-side-inset must be at least 0 and less than 0.5")

    NEAR_X_GAIN = args.near_x_gain
    NEAR_Y_GAIN = args.near_y_gain
    FAR_X_GAIN = args.far_x_gain
    FAR_Y_GAIN = args.far_y_gain
    FULL_SPEED_ERROR_PX = args.full_speed_error
    ACCEL_PULSES = args.accel_pulses
    BODY_SIDE_INSET = args.body_side_inset
    AIM_BODY = args.aim_body

    # Remove the adaptive-only flags, then let the proven controller parse all
    # existing networking, direction, safety, and overlay options.
    sys.argv = [
        sys.argv[0],
        *remaining,
        "--x-gain",
        str(NEAR_X_GAIN),
        "--y-gain",
        str(NEAR_Y_GAIN),
    ]
    motion.calculate_motion = calculate_adaptive_motion
    motion.PiMotionClient = AdaptivePiMotionClient
    motion.MotionOverlay = AdaptiveMotionOverlay
    motion.select_aim_target = select_runtime_target

    print(
        f"Adaptive {'body' if AIM_BODY else 'head'} controller: "
        f"near=({NEAR_X_GAIN:g}, {NEAR_Y_GAIN:g}), "
        f"far=({FAR_X_GAIN:g}, {FAR_Y_GAIN:g}), "
        f"full speed at {FULL_SPEED_ERROR_PX:g}px, "
        f"acceleration={ACCEL_PULSES} pulses/command, "
        f"body side inset={BODY_SIDE_INSET:.0%}."
    )
    return motion.main()


if __name__ == "__main__":
    raise SystemExit(main())
