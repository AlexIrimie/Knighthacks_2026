"""Run player detection with a center-reticle alignment indicator."""

import math
import time

from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter

import detection_app


# The green head marker has a 5 px radius. Allow only 3 px beyond it so a
# distant player's torso does not count as a head alignment.
ALIGNMENT_TOLERANCE_PX = 8


def is_reticle_aligned(target, screen_width, screen_height, tolerance_px=ALIGNMENT_TOLERANCE_PX):
    """Return whether target is close enough to the screen-center reticle."""
    if target is None or not screen_width or not screen_height:
        return False
    dx = target[0] - screen_width / 2
    dy = target[1] - screen_height / 2
    return math.hypot(dx, dy) <= tolerance_px


class AlignmentOverlay(detection_app.DetectionOverlay):
    """The existing overlay plus an ON TARGET / NOT ON TARGET status."""

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
        painter.end()


def main():
    # Reuse the current capture, detection, diagnostics, and command-line setup;
    # only replace its overlay window for this separate entry point.
    detection_app.DetectionOverlay = AlignmentOverlay
    return detection_app.main()


if __name__ == "__main__":
    raise SystemExit(main())
