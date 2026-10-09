"""Live capture, diagnostics, raw-frame recording and offline replay."""
import argparse
import csv
import ctypes
from datetime import datetime
import json
from pathlib import Path
import queue
import signal
import sys
import threading
import time

import cv2
from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from player_detection import DetectionConfig, PlayerDetector, debug_image

TARGET_CAPTURE_FPS = 120
OUTPUT_IDX = 0
DEBUG_FPS = 15
OVERLAY_REFRESH_MS = 8
CONFIG = DetectionConfig()
state_lock = threading.Lock()
stop_event = threading.Event()
shared_state = {"running": True, "frame_number": 0}


class SampleRecorder:
    """Bounded background PNG writer; disk congestion drops samples."""
    def __init__(self, directory, seconds, fps):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.seconds, self.interval = seconds, 1 / fps
        self.next_sample = 0.0
        self.dropped = 0
        self.error = None
        self.items = queue.Queue(maxsize=4)
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self._write, daemon=True)
        self.worker.start()

    def submit(self, frame, elapsed):
        if elapsed > self.seconds or elapsed < self.next_sample or self.error:
            return
        self.next_sample = elapsed + self.interval
        try:
            self.items.put_nowait((elapsed, frame))
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            with (self.directory / "frames.jsonl").open("w", encoding="utf-8") as manifest:
                index = 0
                while not self.finished.is_set() or not self.items.empty():
                    try:
                        elapsed, frame = self.items.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    name = f"{index:05d}.png"
                    if not cv2.imwrite(str(self.directory / name), frame, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                        raise OSError(f"Could not write {name}")
                    manifest.write(json.dumps({"file": name, "time_seconds": elapsed}) + "\n")
                    manifest.flush()
                    index += 1
        except Exception as exc:
            self.error = str(exc)

    def close(self):
        self.finished.set()
        self.worker.join()
        print(f"Sample saved: {self.directory} (dropped samples: {self.dropped})")
        if self.error:
            print(f"Recording error: {self.error}")


METRIC_FIELDS = ["frame", "time_seconds", "detected", "candidate_count", "box_x", "box_y",
                 "box_width", "box_height", "target_x", "target_y", "dx_from_center",
                 "dy_from_center", "vision_processing_ms", "loop_fps", "capture_call_ms",
                 "publish_ms", "previous_csv_write_ms", "debug_ms", "record_submit_ms", "confidence",
                 "status", "wide_search", "components_truncated", "rejection_reasons"]


def vision_worker(args):
    camera = recorder = None
    timer_resolution = False
    error = None
    try:
        import dxcam
        camera = dxcam.create(output_idx=OUTPUT_IDX, output_color="BGR")
        # Pair the request with timeEndPeriod in finally. This improves timed
        # waits on Windows/Python versions with coarse default timer resolution.
        timer_resolution = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        detector = PlayerDetector(CONFIG)
        if args.record_seconds:
            directory = Path("captures") / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            recorder = SampleRecorder(directory, args.record_seconds, args.record_fps)
        with Path(args.csv).open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=METRIC_FIELDS)
            writer.writeheader()
            start = next_capture = last_flush = time.perf_counter()
            last_debug = -1.0
            last_frame_time = None
            csv_ms = 0.0
            frame_number = 0
            while not stop_event.is_set():
                # No application frame backlog. Slow processing reduces effective FPS.
                if stop_event.wait(max(0, next_capture - time.perf_counter())):
                    break
                capture_start = time.perf_counter()
                next_capture = capture_start + 1 / TARGET_CAPTURE_FPS
                frame = camera.grab()
                received = time.perf_counter()
                capture_ms = (received - capture_start) * 1000
                if frame is None:
                    continue
                frame_number += 1
                elapsed = received - start
                want_debug = args.debug and received - last_debug >= 1 / DEBUG_FPS
                result = detector.process(frame, received, debug=want_debug)
                detected_at = time.perf_counter()
                vision_ms = (detected_at - received) * 1000
                fps = 1 / (received - last_frame_time) if last_frame_time is not None else 0.0
                last_frame_time = received
                target = result["target"]
                height, width = frame.shape[:2]
                # Publish before optional diagnostics, logging and recording.
                with state_lock:
                    shared_state.update(result)
                    shared_state.update(screen_width=width, screen_height=height,
                                        frame_number=frame_number, vision_ms=vision_ms,
                                        loop_fps=fps, capture_ms=capture_ms,
                                        received_at=received, published_at=time.perf_counter())
                publish_ms = (time.perf_counter() - detected_at) * 1000
                debug_start = time.perf_counter()
                if want_debug:
                    panel = debug_image(frame, result)
                    with state_lock:
                        shared_state["debug_panel"] = panel
                    last_debug = received
                debug_ms = (time.perf_counter() - debug_start) * 1000
                record_start = time.perf_counter()
                if recorder:
                    recorder.submit(frame, elapsed)
                record_ms = (time.perf_counter() - record_start) * 1000
                box = result["best_box"] or ("",) * 4
                row = dict(zip(METRIC_FIELDS, [
                    frame_number, elapsed, int(target is not None), len(result["candidates"]),
                    *box, *(target or ("", "")),
                    target[0] - width // 2 if target else "",
                    target[1] - height // 2 if target else "", vision_ms, fps,
                    capture_ms, publish_ms, csv_ms, debug_ms, record_ms,
                    result["confidence"], result["status"], int(result["wide_search"]),
                    int(result["truncated"]),
                    ";".join(sorted({c["reason"] for c in result["candidates"]
                                     if c["reason"] != "accepted"}))]))
                if not args.no_csv:
                    csv_start = time.perf_counter()
                    writer.writerow(row)
                    if received - last_flush >= 1:
                        output.flush()
                        last_flush = received
                    csv_ms = (time.perf_counter() - csv_start) * 1000
    except Exception as exc:
        error = str(exc)
        print(f"Vision error: {error}")
    finally:
        for resource, method in ((camera, "release"), (recorder, "close")):
            try:
                if resource is not None:
                    getattr(resource, method)()
            except Exception as exc:
                error = error or str(exc)
                print(f"Cleanup error: {exc}")
        if timer_resolution:
            ctypes.windll.winmm.timeEndPeriod(1)
        with state_lock:
            shared_state.update(running=False, error=error)


def exclude_from_capture(widget):
    # Keep diagnostics and drawn boxes out of the pixels being analyzed.
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowDisplayAffinity.restype = ctypes.c_int
        success = user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11)
    except (AttributeError, OSError):
        success = False
    if not success:
        print("Capture exclusion unavailable; keep diagnostics on another monitor.")


class DetectionOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Player detection overlay")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(QApplication.primaryScreen().geometry())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(OVERLAY_REFRESH_MS)
        self.show()
        exclude_from_capture(self)

    def refresh(self):
        with state_lock:
            running = shared_state["running"]
        if not running:
            QApplication.quit()
        self.update()

    def paintEvent(self, event):
        with state_lock:
            data = dict(shared_state)
        if not data["frame_number"]:
            return
        width, height = data["screen_width"], data["screen_height"]
        sx, sy = self.width() / width, self.height() / height
        age_ms = (time.perf_counter() - data["received_at"]) * 1000
        fresh = age_ms < CONFIG.lost_seconds * 1000
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if data["best_box"] is not None and fresh:
            x, y, w, h = data["best_box"]
            painter.setPen(QPen(QColor(255, 40, 40), 2))
            painter.drawRect(QRectF(x * sx, y * sy, w * sx, h * sy))
            tx, ty = data["target"]
            painter.setPen(QPen(QColor(0, 255, 80), 2))
            painter.drawEllipse(QRectF(tx * sx - 5, ty * sy - 5, 10, 10))
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Consolas", 11))
        status = data["status"] if fresh else "no fresh frame"
        stats = (f"{status}  confidence {data['confidence']:.2f}  CV {data['vision_ms']:.2f} ms  "
                 f"{data['loop_fps']:.0f} FPS / cap 120  age {age_ms:.0f} ms  "
                 f"{'wide' if data['wide_search'] else 'local'}")
        painter.drawText(20, 30, stats)
        painter.end()


class DebugWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Detection diagnostics — raw / cleaned / candidates")
        self.label = QLabel("Waiting for a frame…")
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        self.last_panel = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(round(1000 / DEBUG_FPS))
        self.show()
        exclude_from_capture(self)

    def refresh(self):
        with state_lock:
            panel = shared_state.get("debug_panel")
        if panel is None or panel is self.last_panel:
            return
        self.last_panel = panel
        h, w = panel.shape[:2]
        image = QImage(panel.data, w, h, panel.strides[0], QImage.Format.Format_BGR888).copy()
        self.label.setPixmap(QPixmap.fromImage(image).scaledToWidth(1200, Qt.TransformationMode.FastTransformation))


def replay(args):
    directory = Path(args.replay)
    detector = PlayerDetector(CONFIG)
    rows = []
    debug_directory = directory / "debug"
    if args.debug:
        debug_directory.mkdir(exist_ok=True)
    with (directory / "frames.jsonl").open(encoding="utf-8") as manifest:
        for index, line in enumerate(manifest):
            item = json.loads(line)
            frame = cv2.imread(str(directory / item["file"]))
            if frame is None:
                raise ValueError(f"Unreadable frame: {item['file']}")
            started = time.perf_counter()
            result = detector.process(frame, item["time_seconds"], debug=args.debug)
            ms = (time.perf_counter() - started) * 1000
            rows.append({"frame": index, "time_seconds": item["time_seconds"],
                         "detected": int(result["target"] is not None),
                         "box": result["best_box"], "confidence": result["confidence"],
                         "status": result["status"], "vision_processing_ms": ms})
            if args.debug:
                if not cv2.imwrite(str(debug_directory / item["file"]), debug_image(frame, result)):
                    raise OSError("Could not save replay diagnostic image")
    if not rows:
        raise ValueError("Recording contains no frames")
    destination = Path(args.csv)
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Replayed {len(rows)} frames; metrics: {destination.resolve()}")
    if args.debug:
        print(f"Diagnostic images: {debug_directory.resolve()}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", help="Show masks/candidates; save images during replay")
    parser.add_argument("--record-seconds", type=float, default=0, help="Record raw frames at startup for this duration")
    parser.add_argument("--record-fps", type=float, default=30, help="Sample rate, capped at 120 (default 30)")
    parser.add_argument("--replay", help="Replay a captures/session directory without opening the overlay")
    parser.add_argument("--csv", help="Metrics destination; default is a unique file under runs/")
    parser.add_argument("--no-csv", action="store_true", help="Disable per-frame live CSV writes for comparison")
    args = parser.parse_args()
    if args.record_seconds < 0 or not 0 < args.record_fps <= TARGET_CAPTURE_FPS:
        parser.error("record-seconds must be >= 0; record-fps must be > 0 and <= 120")
    if args.csv is None:
        Path("runs").mkdir(exist_ok=True)
        args.csv = str(Path("runs") / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".csv"))
    if args.replay:
        return replay(args)
    print(f"Detection cap: {TARGET_CAPTURE_FPS} FPS. Use Borderless / Windowed Fullscreen.")
    print(f"Metrics: {Path(args.csv).resolve()}; Ctrl+C in this terminal to stop.")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    stop_event.clear()
    with state_lock:
        shared_state.update(running=True, frame_number=0)

    def shutdown(*unused):
        stop_event.set()
        app.quit()

    signal.signal(signal.SIGINT, shutdown)
    app.aboutToQuit.connect(stop_event.set)
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(100)
    overlay = DetectionOverlay()
    debug_window = DebugWindow() if args.debug else None
    worker = threading.Thread(target=vision_worker, args=(args,), daemon=True)
    worker.start()
    result = app.exec()
    stop_event.set()
    worker.join(timeout=5)
    if worker.is_alive():
        print("Capture/recording worker did not finish within five seconds.")
        return 1
    with state_lock:
        error = shared_state.get("error")
    print("Stopped.")
    return 1 if error else result


if __name__ == "__main__":
    sys.exit(main())
