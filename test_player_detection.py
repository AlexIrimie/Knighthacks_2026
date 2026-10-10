"""Synthetic regression checks; these do not establish real-game accuracy."""
import csv
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from player_detection import DetectionConfig, PlayerDetector, debug_image


PURPLE = tuple(int(v) for v in cv2.cvtColor(np.uint8([[[145, 220, 240]]]), cv2.COLOR_HSV2BGR)[0, 0])


def frame_with_players(*players):
    frame = np.zeros((360, 640, 3), np.uint8)
    for x, y, scale in players:
        # Head, shoulders, arms, torso, two legs; one-pixel outline survives.
        points = np.array([(15, 0), (25, 0), (25, 14), (34, 18), (40, 50),
                           (31, 52), (29, 38), (29, 100), (21, 100), (20, 66),
                           (18, 100), (10, 100), (10, 38), (8, 52), (0, 50),
                           (6, 18), (15, 14)], dtype=float)
        points = np.rint(points * scale + [x, y]).astype(np.int32)
        cv2.polylines(frame, [points], True, PURPLE, 1)
    return frame


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.detector = PlayerDetector(DetectionConfig(search_fraction=1.0))

    def acquire(self, frame, start=0):
        results = [self.detector.process(frame, start + i / 120, debug=True) for i in range(4)]
        self.assertIsNotNone(results[-1]["best_box"])
        return results[-1]

    def test_thin_outline_survives(self):
        frame = frame_with_players((290, 120, 1))
        result = self.acquire(frame)
        self.assertGreater(result["best_box"][3], 95)
        self.assertEqual(cv2.countNonZero(result["raw_mask"]), cv2.countNonZero(cv2.inRange(
            cv2.cvtColor(frame[result["region"][1]:result["region"][3], result["region"][0]:result["region"][2]],
                         cv2.COLOR_BGR2HSV), self.detector.lower, self.detector.upper)))
        self.assertEqual(debug_image(frame, result).shape, (390, 1440, 3))

    def test_stacked_fragments_group(self):
        frame = np.zeros((360, 640, 3), np.uint8)
        cv2.rectangle(frame, (302, 105), (320, 125), PURPLE, 1)
        cv2.rectangle(frame, (290, 132), (330, 207), PURPLE, 1)
        result = self.acquire(frame)
        self.assertLessEqual(result["best_box"][1], 106)
        self.assertGreaterEqual(result["best_box"][3], 100)
        self.assertTrue(any(c["parts"] == 2 for c in result["candidates"]))

    def test_adjacent_players_remain_separate(self):
        self.detector.config.wide_search_seconds = 0
        result = self.acquire(frame_with_players((265, 120, 1), (312, 120, 1)))
        valid = [c for c in result["candidates"] if c["reason"] == "accepted"]
        self.assertEqual(len(valid), 2)
        self.assertTrue(all(c["box"][2] < 45 for c in valid))

    def test_search_edge_fragments_are_uncertain(self):
        frame = frame_with_players((0, 120, 1))
        result = self.detector.process(frame, 0)
        self.assertIsNone(result["target"])
        self.assertEqual(result["candidates"][0]["reason"], "clipped by search")

    def test_arm_does_not_replace_full_body(self):
        self.acquire(frame_with_players((290, 120, 1)))
        fragment = np.zeros((360, 640, 3), np.uint8)
        cv2.rectangle(fragment, (312, 145), (322, 170), PURPLE, 1)
        result = self.detector.process(fragment, 4 / 120)
        self.assertIsNone(result["best_box"])
        self.assertEqual(result["status"], "temporarily lost")
        restored = self.detector.process(frame_with_players((290, 120, 1)), 5 / 120)
        self.assertIsNotNone(restored["best_box"])

    def test_isolated_uniform_arm_is_uncertain(self):
        frame = np.zeros((360, 640, 3), np.uint8)
        cv2.rectangle(frame, (302, 140), (315, 180), PURPLE, 1)
        for i in range(5):
            result = self.detector.process(frame, i / 120)
            self.assertIsNone(result["target"])
        self.assertEqual(result["candidates"][0]["reason"], "no shoulder shape")

    def test_distant_player_acquires_at_small_scale(self):
        frame = frame_with_players((290, 120, 0.20))
        result = self.acquire(frame)
        self.assertLess(result["best_box"][3], 25)
        self.assertGreater(result["confidence"], 0.52)

    def test_motion_keeps_box_and_retries_wide_same_frame(self):
        self.acquire(frame_with_players((180, 120, 1)))
        result = self.detector.process(frame_with_players((350, 120, 1)), 4 / 120)
        self.assertTrue(result["wide_search"])
        self.assertEqual(result["status"], "tracking")
        self.assertEqual(result["best_box"][0], 350)

    def test_brief_shape_degradation_keeps_full_sized_track(self):
        self.acquire(frame_with_players((290, 120, 1)))
        blurred = np.zeros((360, 640, 3), np.uint8)
        cv2.rectangle(blurred, (294, 120), (334, 220), PURPLE, 1)
        result = self.detector.process(blurred, 4 / 120)
        self.assertEqual(result["status"], "tracking weak")
        self.assertIsNotNone(result["best_box"])
        for i in range(5, 15):
            result = self.detector.process(blurred, i / 120)
        self.assertIsNone(result["best_box"])

    def test_small_fragment_keeps_previous_box_briefly(self):
        full = frame_with_players((290, 120, 0.35))
        previous = self.acquire(full)["best_box"]
        fragment = np.zeros_like(full)
        cv2.rectangle(fragment, (293, 129), (301, 144), PURPLE, 1)
        result = self.detector.process(fragment, 4 / 120)
        self.assertEqual(result["status"], "tracking fragment")
        self.assertEqual(result["best_box"][2:], previous[2:])
        self.assertEqual(result["best_box"][1], previous[1])
        self.assertLess(result["raw_box"][2], previous[2])
        for i in range(5, 15):
            result = self.detector.process(fragment, i / 120)
        self.assertIsNone(result["best_box"])

    def test_display_prediction_is_capped_and_optional(self):
        from detection_app import predicted_shift
        box = (100, 100, 30, 40)
        self.assertEqual(predicted_shift(None, box, .01), (0, 0))
        self.assertEqual(predicted_shift((1000, 0), box, .01), (10, 0))
        self.assertEqual(predicted_shift((10000, 0), box, .01), (30, 0))
        self.assertEqual(predicted_shift((1000, 0), box, .1), (0, 0))

    def test_motion_velocity_stops_on_direction_change(self):
        self.acquire(frame_with_players((290, 120, 1)))
        self.detector.process(frame_with_players((294, 120, 1)), 4 / 120)
        forward = self.detector.process(frame_with_players((300, 120, 1)), 5 / 120)
        self.assertGreater(forward["motion_velocity"][0], 0)
        reversed_frame = self.detector.process(frame_with_players((297, 120, 1)), 6 / 120)
        self.assertIsNone(reversed_frame["motion_velocity"])

    def test_no_stale_target_and_track_expires(self):
        self.acquire(frame_with_players((290, 120, 1)))
        blank = np.zeros((360, 640, 3), np.uint8)
        result = self.detector.process(blank, 0.05)
        self.assertIsNone(result["target"])
        self.detector.process(blank, 0.2)
        self.assertIsNone(self.detector.track)

    def test_larger_candidate_does_not_steal_track(self):
        self.acquire(frame_with_players((290, 120, 1)))
        self.detector.last_wide = -1
        result = self.detector.process(frame_with_players((290, 120, 1), (380, 80, 2)), 4 / 120)
        self.assertLess(result["best_box"][0], 330)

    def test_capture_gap_requires_reconfirmation(self):
        frame = frame_with_players((290, 120, 1))
        self.acquire(frame)
        result = self.detector.process(frame, 1.0)
        self.assertIsNone(result["target"])
        self.assertEqual(result["status"], "confirming")

    def test_single_frame_false_positive_not_published(self):
        result = self.detector.process(frame_with_players((290, 120, 1)), 0)
        self.assertIsNone(result["target"])
        result = self.detector.process(np.zeros((360, 640, 3), np.uint8), 1 / 120)
        self.assertEqual(result["status"], "no target")

    def test_small_jitter_filtered_large_motion_responsive(self):
        self.acquire(frame_with_players((290, 120, 1)))
        result = self.detector.process(frame_with_players((294, 120, 1)), 4 / 120)
        self.assertEqual(result["raw_box"][0], 294)
        self.assertEqual(result["best_candidate_box"][0], 294)
        self.assertGreater(result["best_box"][0], 290)
        self.assertLess(result["best_box"][0], 294)
        result = self.detector.process(frame_with_players((314, 120, 1)), 5 / 120)
        self.assertEqual(result["best_box"][0], 314)

    def test_off_color_noise_rejected(self):
        frame = np.zeros((360, 640, 3), np.uint8)
        cv2.rectangle(frame, (290, 120), (330, 220), (0, 255, 0), 2)
        self.assertEqual(self.detector.process(frame, 0)["candidates"], [])

    def test_resolution_change_resets_track(self):
        self.acquire(frame_with_players((290, 120, 1)))
        self.detector.process(np.zeros((720, 1280, 3), np.uint8), 0.04)
        self.assertIsNone(self.detector.track)

    def test_capture_loop_metrics_and_cap(self):
        import detection_app
        frame = frame_with_players((290, 120, 1))

        class FakeCamera:
            def __init__(self):
                self.calls = []
                self.released = False

            def grab(self):
                self.calls.append(time.perf_counter())
                if len(self.calls) == 5:
                    detection_app.stop_event.set()
                return frame.copy()

            def release(self):
                self.released = True

        camera = FakeCamera()
        detection_app.stop_event.clear()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "live.csv"
            args = SimpleNamespace(record_seconds=0, debug=True, csv=str(output), no_csv=False)
            with patch("dxcam.create", return_value=camera):
                detection_app.vision_worker(args)
            with output.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[-1]["detected"], "1")
            self.assertEqual(rows[-1]["raw_box_x"], "290")
            self.assertGreater(float(rows[-1]["published_seconds"]),
                               float(rows[-1]["time_seconds"]))
            self.assertIn(rows[-1]["search_retry"], ("0", "1"))
            self.assertTrue(all(float(r["capture_call_ms"]) >= 0 for r in rows))
            self.assertTrue(all(b - a >= 1 / 120 - 0.0001 for a, b in zip(camera.calls, camera.calls[1:])))
            self.assertTrue(camera.released)
            self.assertIsNone(detection_app.shared_state["error"])
        detection_app.stop_event.clear()

    def test_record_and_replay(self):
        from detection_app import SampleRecorder, replay
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "sample"
            recorder = SampleRecorder(directory, 1, 30)
            for i in range(3):
                recorder.submit(frame_with_players((290 + i, 120, 1)), i / 30)
            recorder.close()
            manifest = [json.loads(line) for line in (directory / "frames.jsonl").read_text().splitlines()]
            self.assertEqual(len(manifest), 3)
            self.assertIn("live_detection", manifest[0])
            output = Path(temp) / "metrics.csv"
            replay(SimpleNamespace(replay=str(directory), debug=True, csv=str(output)))
            with output.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[-1]["detected"], "1")
            self.assertEqual(len(list((directory / "debug").glob("*.png"))), 3)

    def test_lossless_recording_preserves_outline_pixels(self):
        from detection_app import SampleRecorder
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "exact"
            original = frame_with_players((290, 120, .35))
            recorder = SampleRecorder(directory, 1, 30, image_format="png")
            recorder.submit(original, 0)
            recorder.close()
            saved = cv2.imread(str(directory / "00000.png"))
            self.assertTrue(np.array_equal(original, saved))

    def test_buffered_capture_returns_latest_without_queue(self):
        from detection_app import BufferedCapture, stop_event

        class FakeCamera:
            def __init__(self):
                self.stopped = False
                self.index = 0

            def start(self, **kwargs):
                self.options = kwargs

            def get_latest_frame(self):
                time.sleep(.003)
                self.index += 1
                return self.index

            def stop(self):
                self.stopped = True

        stop_event.clear()
        camera = FakeCamera()
        capture = BufferedCapture(camera)
        try:
            first, _, _ = capture.next_frame()
            time.sleep(.1)
            latest, _, _ = capture.next_frame()
            self.assertGreater(latest - first, 2)
            self.assertFalse(camera.options["video_mode"])
        finally:
            stop_event.set()
            capture.stop()
            stop_event.clear()
        self.assertFalse(capture.thread.is_alive())

    def test_buffered_worker_processes_and_stops(self):
        import detection_app
        frame = frame_with_players((290, 120, 1))

        class FakeCamera:
            def __init__(self):
                self.calls = 0
                self.released = False

            def start(self, **kwargs):
                self.options = kwargs

            def get_latest_frame(self):
                time.sleep(.01)
                self.calls += 1
                if self.calls >= 8:
                    detection_app.stop_event.set()
                return frame.copy()

            def stop(self):
                pass

            def release(self):
                self.released = True

        camera = FakeCamera()
        detection_app.stop_event.clear()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "buffered.csv"
            args = SimpleNamespace(record_seconds=0, debug=False, csv=str(output), no_csv=False,
                                   capture_mode="buffered", paint_mode="signal", predict_display=False)
            with patch("dxcam.create", return_value=camera):
                detection_app.vision_worker(args)
            with output.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertGreater(len(rows), 0)
            self.assertEqual(rows[-1]["capture_mode"], "buffered")
            self.assertTrue(camera.released)
            self.assertIsNone(detection_app.shared_state["error"])
        detection_app.stop_event.clear()

    def test_profile_paint_log_joins_frame_metrics(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PyQt6.QtWidgets import QApplication
        from detection_app import DetectionOverlay, METRIC_FIELDS, profile_report, shared_state, state_lock

        app = QApplication.instance() or QApplication([])
        detector = PlayerDetector()
        frame = frame_with_players((290, 120, 1))
        for i in range(4):
            result = detector.process(frame, i / 120)
        start = time.perf_counter() - 0.1
        with state_lock:
            shared_state.update(result)
            shared_state.update(running=True, frame_number=4, screen_width=640, screen_height=360,
                                received_at=start + 0.08, capture_attempt_at=start + 0.075,
                                published_at=start + 0.085, session_start=start,
                                loop_fps=120.0, vision_ms=1.0)
        overlay = DetectionOverlay(profile=True)
        app.processEvents()
        overlay.grab()
        self.assertTrue(overlay.paint_samples)
        with state_lock:
            shared_state.update(frame_number=5, motion_velocity=(1000.0, 0.0),
                                capture_attempt_at=time.perf_counter() - .01,
                                received_at=time.perf_counter() - .006,
                                published_at=time.perf_counter() - .003)
        overlay.predict_display = True
        overlay.frame_ready.emit()
        app.processEvents()
        overlay.grab()
        self.assertTrue(any(sample[0] == 5 and sample[-2] > 0
                            for sample in overlay.paint_samples))
        with tempfile.TemporaryDirectory() as temp:
            metrics = Path(temp) / "run.csv"
            with metrics.open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=METRIC_FIELDS)
                writer.writeheader()
                writer.writerow({"frame": 4, "time_seconds": .08, "capture_call_ms": 5,
                                 "vision_processing_ms": 1, "raw_box_x": 290,
                                 "raw_box_y": 120, "box_x": 290, "box_y": 120,
                                 "search_retry": 0, "status": "tracking"})
            paint = overlay.save_profile(metrics)
            with paint.open(newline="") as stream:
                paints = list(csv.DictReader(stream))
            self.assertEqual(paints[0]["frame"], "4")
            self.assertGreaterEqual(float(paints[0]["publish_to_paint_ms"]), 0)
            self.assertIn("Frames painted at least once: 1/1", profile_report(metrics, paint))
        overlay.close()


if __name__ == "__main__":
    unittest.main()
