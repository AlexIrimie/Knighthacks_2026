"""Run player detection with a center-reticle alignment indicator."""

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


# The green head marker has a 5 px radius. Allow only 3 px beyond it so a
# distant player's torso does not count as a head alignment.
ALIGNMENT_TOLERANCE_PX = 8
DEFAULT_PI_HOST = "raspberrypi.local"
DEFAULT_PI_PORT = 8765
HEARTBEAT_SECONDS = 0.25
RECONNECT_SECONDS = 1.0
PI_LED_URI = f"ws://{DEFAULT_PI_HOST}:{DEFAULT_PI_PORT}"


def is_reticle_aligned(target, screen_width, screen_height, tolerance_px=ALIGNMENT_TOLERANCE_PX):
    """Return whether target is close enough to the screen-center reticle."""
    if target is None or not screen_width or not screen_height:
        return False
    dx = target[0] - screen_width / 2
    dy = target[1] - screen_height / 2
    return math.hypot(dx, dy) <= tolerance_px


class PiLedClient:
    """Send the latest alignment state without blocking the Qt paint thread."""

    def __init__(self, uri):
        self.uri = uri
        self._condition = threading.Condition()
        self._desired = False
        self._stopping = False
        self._connected = False
        self._last_error = None
        self._thread = threading.Thread(target=self._run, name="pi-led-client", daemon=True)
        self._thread.start()

    @property
    def connected(self):
        with self._condition:
            return self._connected

    def set_on_target(self, on_target):
        with self._condition:
            on_target = bool(on_target)
            if on_target != self._desired:
                self._desired = on_target
                self._condition.notify_all()

    def close(self):
        with self._condition:
            self._desired = False
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=2)

    def _set_connected(self, connected):
        with self._condition:
            changed = connected != self._connected
            self._connected = connected
        if changed:
            print(f"Pi LED {'connected' if connected else 'disconnected'}: {self.uri}")

    def _wait_to_retry(self):
        with self._condition:
            self._condition.wait_for(lambda: self._stopping, timeout=RECONNECT_SECONDS)
            return self._stopping

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
                    self._last_error = None
                    self._set_connected(True)
                    while True:
                        with self._condition:
                            desired = self._desired
                            stopping = self._stopping
                        websocket.send(json.dumps({"on_target": desired}))
                        if stopping:
                            return
                        with self._condition:
                            self._condition.wait_for(
                                lambda: self._stopping or self._desired != desired,
                                timeout=HEARTBEAT_SECONDS,
                            )
            except Exception as exc:
                self._set_connected(False)
                error = f"{type(exc).__name__}: {exc}"
                if error != self._last_error:
                    print(f"Pi LED unavailable ({error}); retrying every {RECONNECT_SECONDS:g}s.")
                    self._last_error = error
                if self._wait_to_retry():
                    return
            finally:
                self._set_connected(False)


class AlignmentOverlay(detection_app.DetectionOverlay):
    """The existing overlay plus an ON TARGET / NOT ON TARGET status."""

    def __init__(self, *args, **kwargs):
        self.led_client = PiLedClient(PI_LED_URI)
        super().__init__(*args, **kwargs)
        detection_app.QApplication.instance().aboutToQuit.connect(self.led_client.close)

    def paintEvent(self, event):
        super().paintEvent(event)

        with detection_app.state_lock:
            data = dict(detection_app.shared_state)
        if not data.get("frame_number"):
            return

        age_ms = (time.perf_counter() - data["received_at"]) * 1000
        fresh = age_ms < detection_app.CONFIG.lost_seconds * 1000
        aligned = fresh and is_reticle_aligned(
            data.get("target"),
            data.get("screen_width"),
            data.get("screen_height"),
        )
        self.led_client.set_on_target(aligned)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(QFont("Consolas", 18, QFont.Weight.Bold))

        box_width = 260
        box_height = 46
        status_box = QRectF((self.width() - box_width) / 2, 20, box_width, box_height)
        background = QColor(0, 120, 40, 210) if aligned else QColor(150, 20, 20, 210)
        painter.fillRect(status_box, background)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            status_box,
            Qt.AlignmentFlag.AlignCenter,
            "ON TARGET" if aligned else "NOT ON TARGET",
        )
        painter.setFont(QFont("Consolas", 10))
        painter.drawText(
            QRectF(status_box.left(), status_box.bottom() + 3, box_width, 22),
            Qt.AlignmentFlag.AlignCenter,
            "PI CONNECTED" if self.led_client.connected else "PI OFFLINE",
        )
        painter.end()


def main():
    global PI_LED_URI
    pi_parser = argparse.ArgumentParser(add_help=False)
    pi_parser.add_argument("--pi-host", default=DEFAULT_PI_HOST)
    pi_parser.add_argument("--pi-port", type=int, default=DEFAULT_PI_PORT)
    pi_args, remaining = pi_parser.parse_known_args()
    if not 1 <= pi_args.pi_port <= 65535:
        pi_parser.error("--pi-port must be between 1 and 65535")
    if connect is None:
        print("Missing WebSocket dependency. Install it with: pip install 'websockets>=15,<18'")
        return 1
    PI_LED_URI = f"ws://{pi_args.pi_host}:{pi_args.pi_port}"
    sys.argv = [sys.argv[0], *remaining]

    # Reuse the current capture, detection, diagnostics, and command-line setup;
    # only replace its overlay window for this separate entry point.
    detection_app.DetectionOverlay = AlignmentOverlay
    return detection_app.main()


if __name__ == "__main__":
    raise SystemExit(main())
