"""Live capture, diagnostics, raw-frame recording and offline replay."""
import argparse
import csv
import ctypes
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import signal
import statistics
import sys
import threading
import time

import cv2
from PyQt6.QtCore import Qt, QTimer, QRectF, pyqtSignal
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
    """Bounded background image writer; disk congestion drops samples."""
    def __init__(self, directory, seconds, fps, image_format="jpg"):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=False)
        self.seconds, self.interval = seconds, 1 / fps
        self.image_format = image_format
        self.next_sample = 0.0
        self.dropped = 0
        self.error = None
        self.items = queue.Queue(maxsize=4)
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self._write, daemon=True)
        self.worker.start()

    def submit(self, frame, elapsed, diagnostic=None):
        if elapsed > self.seconds or elapsed < self.next_sample or self.error:
            return
        self.next_sample = elapsed + self.interval
        try:
            self.items.put_nowait((elapsed, frame, diagnostic))
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            with (self.directory / "frames.jsonl").open("w", encoding="utf-8") as manifest:
                index = 0
                while not self.finished.is_set() or not self.items.empty():
                    try:
                        elapsed, frame, diagnostic = self.items.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    name = f"{index:05d}.{self.image_format}"
                    params = ([cv2.IMWRITE_JPEG_QUALITY, 92] if self.image_format == "jpg"
                              else [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    if not cv2.imwrite(str(self.directory / name), frame, params):
                        raise OSError(f"Could not write {name}")
                    manifest.write(json.dumps({"file": name, "time_seconds": elapsed,
                                               "live_detection": diagnostic}) + "\n")
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
                 "status", "wide_search", "components_truncated", "rejection_reasons",
                 "capture_attempt_seconds", "detection_done_seconds", "published_seconds",
                 "raw_box_x", "raw_box_y", "raw_box_width", "raw_box_height",
                 "candidate_box_x", "candidate_box_y", "candidate_box_width", "candidate_box_height",
                 "search_retry", "capture_mode", "paint_mode", "predict_display"]

PAINT_FIELDS = ["frame", "first_for_frame", "paint_start_seconds", "paint_end_seconds",
                "capture_attempt_seconds", "received_seconds", "published_seconds",
                "receive_to_paint_ms", "publish_to_paint_ms", "paint_duration_ms", "status",
                "prediction_dx", "prediction_dy"]


def predicted_shift(velocity, box, age_seconds):
    """Bounded overlay-only extrapolation from two consistent raw movements."""
    if velocity is None or box is None or not 0 <= age_seconds <= 0.05:
        return 0.0, 0.0
    horizon = min(age_seconds, 0.012)
    dx, dy = velocity[0] * horizon, velocity[1] * horizon
    distance = math.hypot(dx, dy)
    limit = min(40.0, box[3] * 0.75)
    if distance > limit:
        scale = limit / distance
        dx, dy = dx * scale, dy * scale
    return dx, dy


def profile_report(metrics_path, paint_path=None):
    """Summarize app-side timing without claiming game render or display latency."""
    metrics_path = Path(metrics_path)
    paint_path = Path(paint_path) if paint_path else metrics_path.with_name(metrics_path.stem + "_paint.csv")
    with metrics_path.open(newline="", encoding="utf-8") as stream:
        frames = list(csv.DictReader(stream))
    if paint_path.exists():
        with paint_path.open(newline="", encoding="utf-8") as stream:
            paints = list(csv.DictReader(stream))
    else:
        paints = []
    if not frames:
        raise ValueError(f"No frame data in {metrics_path}")
    frame_ids = {int(frame["frame"]) for frame in frames}
    first_paints = [p for p in paints if p["first_for_frame"] == "1"
                    and int(p["frame"]) in frame_ids]

    def spread(values):
        if not values:
            return "n/a"
        values = sorted(values)
        return (f"median {statistics.median(values):.1f} ms, "
                f"p95 {values[math.ceil((len(values) - 1) * .95)]:.1f} ms")

    raw_offset = []
    for frame in frames:
        if frame.get("raw_box_x") and frame.get("box_x"):
            dx = float(frame["raw_box_x"]) - float(frame["box_x"])
            dy = float(frame["raw_box_y"]) - float(frame["box_y"])
            raw_offset.append((dx * dx + dy * dy) ** .5)
    duration = float(frames[-1]["time_seconds"]) - float(frames[0]["time_seconds"])
    losses = sum(frames[i - 1]["status"].startswith("tracking") and
                 not frames[i]["status"].startswith("tracking")
                 for i in range(1, len(frames)))
    report = [
        f"Frames processed: {len(frames)} over {duration:.1f} s "
        f"({(len(frames) - 1) / duration:.1f} FPS average)" if duration > 0 else
        f"Frames processed: {len(frames)}",
        f"Capture mode: {frames[0].get('capture_mode') or 'direct'}",
        ("Capture wait: " if frames[0].get("capture_mode") == "buffered" else "Capture call: ")
        + spread([float(f['capture_call_ms']) for f in frames]),
        f"Paint mode: {frames[0].get('paint_mode') or 'timer'}; "
        f"display prediction: {frames[0].get('predict_display') or '0'}",
        f"Detection: {spread([float(f['vision_processing_ms']) for f in frames])}",
        ("Capture wait start to first overlay paint: "
         if frames[0].get("capture_mode") == "buffered" else
         "Capture request to first overlay paint: ")
        + spread([(float(p["paint_start_seconds"]) - float(p["capture_attempt_seconds"])) * 1000
                  for p in first_paints]),
        f"Received frame to first overlay paint: "
        f"{spread([float(p['receive_to_paint_ms']) for p in first_paints])}",
        f"Published result to first overlay paint: "
        f"{spread([float(p['publish_to_paint_ms']) for p in first_paints])}",
        (f"Frames painted at least once: {len(first_paints)}/{len(frames)}"
         if paint_path.exists() else "Overlay paint log: unavailable for this run"),
        f"Raw match to tracked box offset: {spread(raw_offset).replace(' ms', ' px')}",
        (f"Wide retries: {sum(f.get('search_retry') == '1' for f in frames)}"
         if "search_retry" in frames[0] else "Wide retry count: unavailable for this run"),
        f"Tracking-to-missing transitions: {losses}",
        f"Temporary losses: {sum(f['status'] == 'temporarily lost' for f in frames)}",
        "Timing begins when Python requests or receives a desktop frame; "
        "game rendering and final monitor presentation are not measured.",
    ]
    return "\n".join(report)


class BufferedCapture:
    """Consume the newest DXCam frame without queuing old frames for detection."""
    def __init__(self, camera):
        self.camera = camera
        self.condition = threading.Condition()
        self.latest = None
        self.sequence = 0
        self.consumed = 0
        self.error = None
        self.closing = False
        camera.start(target_fps=TARGET_CAPTURE_FPS, video_mode=False)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            while not stop_event.is_set() and not self.closing:
                started = time.perf_counter()
                frame = self.camera.get_latest_frame()
                received = time.perf_counter()
                if frame is None or stop_event.is_set() or self.closing:
                    continue
                with self.condition:
                    self.latest = (frame, started, received)
                    self.sequence += 1
                    self.condition.notify()
        except Exception as exc:
            if not stop_event.is_set() and not self.closing:
                self.error = exc
                with self.condition:
                    self.condition.notify_all()

    def next_frame(self):
        with self.condition:
            while self.sequence == self.consumed and not stop_event.is_set() and not self.closing:
                if self.error:
                    raise RuntimeError("DXCam capture thread failed") from self.error
                self.condition.wait(timeout=0.05)
            if stop_event.is_set() or self.closing:
                return None
            self.consumed = self.sequence
            return self.latest

    def stop(self):
        self.closing = True
        with self.condition:
            self.condition.notify_all()
        self.camera.stop()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError("DXCam frame reader did not stop")


def vision_worker(args, frame_ready=None):
    camera = recorder = None
    buffered = None
    timer_resolution = False
    error = None
    try:
        import dxcam
        camera = dxcam.create(output_idx=OUTPUT_IDX, output_color="BGR")
        # Pair the request with timeEndPeriod in finally. This improves timed
        # waits on Windows/Python versions with coarse default timer resolution.
        timer_resolution = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        if getattr(args, "capture_mode", "direct") == "buffered":
            buffered = BufferedCapture(camera)
        detector = PlayerDetector(CONFIG)
        if args.record_seconds:
            directory = Path("captures") / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            recorder = SampleRecorder(directory, args.record_seconds, args.record_fps,
                                      getattr(args, "record_format", "jpg"))
        with Path(args.csv).open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=METRIC_FIELDS)
            writer.writeheader()
            start = next_capture = last_flush = time.perf_counter()
            with state_lock:
                shared_state["session_start"] = start
            last_debug = -1.0
            last_frame_time = None
            csv_ms = 0.0
            frame_number = 0
            while not stop_event.is_set():
                if buffered is None:
                    if stop_event.wait(max(0, next_capture - time.perf_counter())):
                        break
                    capture_start = time.perf_counter()
                    next_capture = capture_start + 1 / TARGET_CAPTURE_FPS
                    frame = camera.grab()
                    received = time.perf_counter()
                else:
                    item = buffered.next_frame()
                    if item is None:
                        break
                    frame, capture_start, received = item
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
                published_at = time.perf_counter()
                with state_lock:
                    shared_state.update(result)
                    shared_state.update(screen_width=width, screen_height=height,
                                        frame_number=frame_number, vision_ms=vision_ms,
                                        loop_fps=fps, capture_ms=capture_ms,
                                        capture_attempt_at=capture_start,
                                        received_at=received, published_at=published_at)
                if frame_ready is not None:
                    frame_ready.emit()
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
                    recorder.submit(frame, elapsed, {
                        "frame": frame_number, "status": result["status"],
                        "candidate_box": result["best_candidate_box"],
                        "raw_box": result["raw_box"],
                        "tracked_box": result["best_box"]})
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
                row.update(dict(zip(METRIC_FIELDS[-15:-3], [
                    capture_start - start, detected_at - start, published_at - start,
                    *(result["raw_box"] or ("",) * 4),
                    *(result["best_candidate_box"] or ("",) * 4),
                    int(result["search_retry"])])))
                row.update(capture_mode=getattr(args, "capture_mode", "direct"),
                           paint_mode=getattr(args, "paint_mode", "timer"),
                           predict_display=int(getattr(args, "predict_display", False)))
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
        for resource, method in ((buffered, "stop"), (camera, "release"),
                                 (recorder, "close")):
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
    frame_ready = pyqtSignal()

    def __init__(self, profile=False, paint_mode="signal", predict_display=False):
        super().__init__()
        self.profile = profile
        self.paint_mode = paint_mode
        self.predict_display = predict_display
        self.paint_samples = []
        self.last_sampled_frame = None
        self.setWindowTitle("Player detection overlay")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(QApplication.primaryScreen().geometry())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(OVERLAY_REFRESH_MS if paint_mode == "timer" else 50)
        if paint_mode == "signal":
            self.frame_ready.connect(self.on_frame_ready)
        self.show()
        exclude_from_capture(self)

    def refresh(self):
        with state_lock:
            running = shared_state["running"]
        if not running:
            QApplication.quit()
        self.update()

    def on_frame_ready(self):
        self.update()

    def paintEvent(self, event):
        paint_start = time.perf_counter()
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
        shift_x, shift_y = (predicted_shift(data["motion_velocity"], data["best_box"],
                                            paint_start - data["capture_attempt_at"])
                            if self.predict_display and fresh else (0.0, 0.0))
        if self.profile and fresh:
            candidate_box = data["best_candidate_box"]
            raw_box = data["raw_box"]
            for box, color, line_width in ((candidate_box, QColor(0, 220, 255), 1),
                                           (raw_box, QColor(255, 220, 0), 2)):
                if box is not None:
                    x, y, w, h = box
                    painter.setPen(QPen(color, line_width))
                    painter.drawRect(QRectF(x * sx, y * sy, w * sx, h * sy))
        if data["best_box"] is not None and fresh:
            x, y, w, h = data["best_box"]
            if self.profile and self.predict_display:
                painter.setPen(QPen(QColor(255, 40, 40), 2))
                painter.drawRect(QRectF(x * sx, y * sy, w * sx, h * sy))
            painter.setPen(QPen(QColor(230, 50, 230) if self.profile and self.predict_display
                                else QColor(255, 40, 40), 2))
            painter.drawRect(QRectF((x + shift_x) * sx, (y + shift_y) * sy, w * sx, h * sy))
            tx, ty = data["target"]
            painter.setPen(QPen(QColor(0, 255, 80), 2))
            painter.drawEllipse(QRectF((tx + shift_x) * sx - 5, (ty + shift_y) * sy - 5, 10, 10))
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Consolas", 11))
        status = data["status"] if fresh else "no fresh frame"
        stats = (f"{status}  confidence {data['confidence']:.2f}  CV {data['vision_ms']:.2f} ms  "
                 f"{data['loop_fps']:.0f} FPS / cap 120  age {age_ms:.0f} ms  "
                 f"{'wide' if data['wide_search'] else 'local'}")
        painter.drawText(20, 30, stats)
        if self.profile:
            legend = ("CYAN candidate   YELLOW raw match   RED tracked box   "
                      + ("MAGENTA display prediction   " if self.predict_display else "")
                      + "GREEN head")
            painter.drawText(20, 52, legend)
        painter.end()
        if self.profile:
            paint_end = time.perf_counter()
            frame_number = data["frame_number"]
            first = frame_number != self.last_sampled_frame
            self.last_sampled_frame = frame_number
            start = data["session_start"]
            self.paint_samples.append((frame_number, int(first), paint_start - start,
                                       paint_end - start, data["capture_attempt_at"] - start,
                                       data["received_at"] - start,
                                       data["published_at"] - start,
                                       (paint_start - data["received_at"]) * 1000,
                                       (paint_start - data["published_at"]) * 1000,
                                       (paint_end - paint_start) * 1000, data["status"],
                                       shift_x, shift_y))

    def save_profile(self, metrics_path):
        destination = Path(metrics_path).with_name(Path(metrics_path).stem + "_paint.csv")
        with destination.open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(PAINT_FIELDS)
            writer.writerows(self.paint_samples)
        return destination


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
                debug_name = Path(item["file"]).stem + ".png"
                if not cv2.imwrite(str(debug_directory / debug_name), debug_image(frame, result)):
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
    parser.add_argument("--record-format", choices=("jpg", "png"), default="jpg",
                        help="JPEG for smooth visual recording; PNG for lossless replay")
    parser.add_argument("--replay", help="Replay a captures/session directory without opening the overlay")
    parser.add_argument("--csv", help="Metrics destination; default is a unique file under runs/")
    parser.add_argument("--no-csv", action="store_true", help="Disable per-frame live CSV writes for comparison")
    parser.add_argument("--profile", action="store_true", help="Compare raw/tracked boxes and log overlay paint timing")
    parser.add_argument("--paint-mode", choices=("signal", "timer"), default="signal",
                        help="Repaint on each result (default) or use the original 8 ms timer")
    parser.add_argument("--capture-mode", choices=("direct", "buffered"), default="direct",
                        help="Use direct grabs (default) or DXCam continuous latest-frame capture")
    parser.add_argument("--predict-display", action="store_true",
                        help="Show a bounded motion-predicted box on the overlay")
    parser.add_argument("--analyze-run", help="Summarize a metrics CSV and its matching _paint.csv")
    args = parser.parse_args()
    if args.record_seconds < 0 or not 0 < args.record_fps <= TARGET_CAPTURE_FPS:
        parser.error("record-seconds must be >= 0; record-fps must be > 0 and <= 120")
    if args.profile and args.no_csv:
        parser.error("--profile requires per-frame CSV; remove --no-csv")
    if args.analyze_run:
        print(profile_report(args.analyze_run))
        return 0
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
    overlay = DetectionOverlay(profile=args.profile, paint_mode=args.paint_mode,
                               predict_display=args.predict_display)
    debug_window = DebugWindow() if args.debug else None
    worker = threading.Thread(target=vision_worker,
                              args=(args, overlay.frame_ready if args.paint_mode == "signal" else None),
                              daemon=True)
    worker.start()
    result = app.exec()
    stop_event.set()
    worker.join(timeout=5)
    if args.profile:
        paint_path = overlay.save_profile(args.csv)
        print(f"Overlay paint timing: {paint_path.resolve()}")
    if worker.is_alive():
        print("Capture/recording worker did not finish within five seconds.")
        return 1
    with state_lock:
        error = shared_state.get("error")
    if args.profile and not worker.is_alive():
        try:
            report = profile_report(args.csv, paint_path)
        except ValueError as exc:
            print(f"Profile summary unavailable: {exc}")
        else:
            summary_path = Path(args.csv).with_name(Path(args.csv).stem + "_profile.txt")
            summary_path.write_text(report + "\n", encoding="utf-8")
            print(report)
            print(f"Profile summary: {summary_path.resolve()}")
    print("Stopped.")
    return 1 if error else result


if __name__ == "__main__":
    sys.exit(main())
