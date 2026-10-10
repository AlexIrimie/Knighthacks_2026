"""Purple-outline detection and temporal selection, independent of capture/UI.

Confidence is a heuristic score, not a probability. Tune on actual range frames.
"""
from dataclasses import dataclass
import math

import cv2
import numpy as np


@dataclass
class DetectionConfig:
    lower_purple: tuple = (125, 70, 80)
    upper_purple: tuple = (165, 255, 255)
    search_fraction: float = 0.90
    min_component_pixels: int = 2
    min_height: int = 10
    min_confidence: float = 0.62
    wide_search_seconds: float = 0.20
    lost_seconds: float = 0.10
    confirm_seconds: float = 0.016
    smooth_seconds: float = 0.012
    head_y_fraction: float = 0.15
    max_components: int = 48


def overlap(a, b):
    x, y = max(a[0], b[0]), max(a[1], b[1])
    r, d = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    intersection = max(0, r - x) * max(0, d - y)
    return intersection / max(1, a[2] * a[3] + b[2] * b[3] - intersection)


def center_distance(a, b):
    return math.hypot(a[0] + a[2] / 2 - b[0] - b[2] / 2,
                      a[1] + a[3] / 2 - b[1] - b[3] / 2)


class PlayerDetector:
    def __init__(self, config=None):
        self.config = config or DetectionConfig()
        self.lower = np.array(self.config.lower_purple, np.uint8)
        self.upper = np.array(self.config.upper_purple, np.uint8)
        # Closing preserves thin strokes; no opening or final dilation.
        self.close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        self.reset()

    def reset(self):
        self.track = None
        self.smooth_box = None
        self.last_seen = -math.inf
        self.last_update = None
        self.last_wide = -math.inf
        self.pending = None
        self.pending_since = 0.0
        self.weak_since = None
        self.last_raw_center = None
        self.last_raw_time = None
        self.motion_steps = []
        self.motion_velocity = None
        self.shape = None

    def _region(self, width, height, now):
        fraction = self.config.search_fraction
        mx, my = int(width * (1 - fraction) / 2), int(height * (1 - fraction) / 2)
        wide = (mx, my, width - mx, height - my)
        # A missed detection immediately requests a wider search next frame.
        if (self.track is None or now - self.last_wide >= self.config.wide_search_seconds
                or self.last_update != self.last_seen):
            self.last_wide = now
            return wide, True
        x, y, w, h = self.track
        pad_x, pad_y = max(144, w * 1.8), max(96, h)
        return (max(mx, int(x - pad_x)), max(my, int(y - pad_y)),
                min(width - mx, int(x + w + pad_x)),
                min(height - my, int(y + h + pad_y))), False

    def _groups(self, components):
        """Conservative unions: no long transitive chains across adjacent players."""
        groups = [[box] for box in components]

        def bounds(group):
            left = min(b[0] for b in group)
            top = min(b[1] for b in group)
            return (left, top, max(b[0] + b[2] for b in group) - left,
                    max(b[1] + b[3] for b in group) - top)

        # Merge closest compatible groups first, with bounds rechecked each time.
        for _ in range(len(components)):
            choice = None
            boxes = [bounds(g) for g in groups]
            for i, a in enumerate(boxes):
                for j in range(i + 1, len(boxes)):
                    b = boxes[j]
                    gap_x = max(0, max(a[0], b[0]) - min(a[0] + a[2], b[0] + b[2]))
                    gap_y = max(0, max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3]))
                    scale = max(a[3], b[3])
                    # Separate substantial, tall silhouettes should stay separate.
                    if (min(a[3], b[3]) > scale * 0.65 and
                            min(a[2], b[2]) > scale * 0.22 and gap_x > 0):
                        continue
                    if gap_x > max(4, scale * 0.16) or gap_y > max(5, scale * 0.22):
                        continue
                    combined = bounds(groups[i] + groups[j])
                    if combined[2] > combined[3] * 1.05:
                        continue
                    # Stacked fragments must roughly share a body centerline.
                    if gap_y and abs(a[0] + a[2] / 2 - b[0] - b[2] / 2) > max(5, combined[2] * 0.6):
                        continue
                    cost = (gap_x + gap_y) / max(1, scale)
                    if choice is None or cost < choice[0]:
                        choice = cost, i, j
            if choice is None:
                break
            _, i, j = choice
            groups[i].extend(groups.pop(j))
        return [(bounds(g), len(g)) for g in groups]

    def _candidate(self, box, count, raw, off_x, off_y):
        x, y, w, h = box
        patch = raw[y:y + h, x:x + w] > 0
        rows = patch.any(axis=1)
        occupied = np.flatnonzero(rows)
        aspect = h / max(w, 1)
        row_coverage = float(rows.mean())
        # Outline evidence must extend across the box, not just one tiny cluster.
        bands = sum(bool(b.any()) for b in np.array_split(patch, 4, axis=0)) / 4
        left = patch.argmax(axis=1)
        right = w - 1 - patch[:, ::-1].argmax(axis=1)
        widths = np.where(rows, right - left + 1, 0)
        bilateral = float(np.mean(widths >= max(3, w * 0.38)))
        top_widths = widths[:max(1, int(h * 0.20))]
        torso_widths = widths[int(h * 0.20):max(2, int(h * 0.65))]
        top_widths = top_widths[top_widths > 0]
        torso_widths = torso_widths[torso_widths > 0]
        head_span = float(np.median(top_widths)) if len(top_widths) else w
        torso_span = float(np.percentile(torso_widths, 80)) if len(torso_widths) else 0
        far = h <= 40
        shoulder_shape = (torso_span >= head_span * (1.05 if far else 1.15) and
                          torso_span - head_span >= (1 if far else max(2, w * 0.08)))
        shape_score = min(1.0, max(0.0, (aspect - 0.85) / 0.65),
                          max(0.0, (6.5 - aspect) / 2.0))
        confidence = (0.35 * shape_score + 0.25 * row_coverage
                      + 0.20 * bands + 0.20 * bilateral)
        if not shoulder_shape:
            # A long arm can have a perfectly plausible bounding-box aspect ratio.
            # Require some head-to-shoulder widening before acquiring a full player.
            confidence *= 0.55
        reason = "accepted"
        if h < self.config.min_height or w < 3 or len(occupied) < 4:
            reason = "small"
        elif aspect < 1.0 or aspect > 6.5:
            reason = "shape"
        elif row_coverage < (0.32 if far else 0.45) or bilateral < (0.14 if far else 0.22):
            reason = "fragment"
        elif not shoulder_shape:
            reason = "no shoulder shape"
        elif confidence < (0.52 if far else self.config.min_confidence):
            reason = "low confidence"
        return {"box": (x + off_x, y + off_y, w, h),
                "confidence": confidence, "reason": reason, "parts": count}

    def _select(self, candidates, now, width, height):
        if self.last_update is not None and now - self.last_update > self.config.lost_seconds:
            self.track = self.smooth_box = self.pending = None
            self.motion_steps = []
            self.motion_velocity = None
            self.last_raw_center = None
        valid = [c for c in candidates if c["reason"] == "accepted"]
        selected = None
        weak_match = False
        fragment_match = False
        if self.track is not None:
            matches = []
            for candidate in candidates:
                box = candidate["box"]
                size_ratio = box[2] * box[3] / max(1, self.track[2] * self.track[3])
                height_ratio = box[3] / self.track[3]
                width_ratio = box[2] / self.track[2]
                distance = center_distance(box, self.track)
                strong = candidate["reason"] == "accepted"
                weak = (candidate["reason"] in ("fragment", "no shoulder shape", "low confidence")
                        and candidate["confidence"] >= 0.40
                        and 0.68 <= size_ratio <= 1.45
                        and 0.75 <= width_ratio <= 1.35
                        and 0.8 <= height_ratio <= 1.25
                        and distance < max(64, self.track[3] * 0.85)
                        and (self.weak_since is None or now - self.weak_since <= 0.05))
                fragment = (candidate["reason"] in
                            ("accepted", "fragment", "no shoulder shape", "low confidence", "shape")
                            and candidate["confidence"] >= 0.45
                            and 0.12 <= size_ratio < 0.50
                            and abs(box[0] + box[2] / 2 - self.track[0] - self.track[2] / 2)
                            < max(24, self.track[2] * 0.65)
                            and abs(box[1] + box[3] / 2 - self.track[1] - self.track[3] / 2)
                            < max(20, self.track[3] * 0.45)
                            and (self.weak_since is None or now - self.weak_since <= 0.05))
                if ((strong and 0.50 <= size_ratio <= 1.9 and
                     0.65 <= height_ratio <= 1.5 and
                     distance < max(192, self.track[3] * 1.3)) or weak or fragment):
                    matches.append((overlap(box, self.track) - distance / max(1, self.track[3])
                                    + candidate["confidence"] * 0.25
                                    + (0.5 if strong and not fragment else 0)
                                    - (0.2 if fragment else 0), candidate, weak, fragment))
            if matches:
                _, selected, weak_match, fragment_match = max(matches, key=lambda item: item[0])
            elif now - self.last_seen <= self.config.lost_seconds:
                # Keep identity briefly, but publish no stale box or target point.
                self.last_update = now
                self.motion_steps = []
                self.motion_velocity = self.last_raw_center = None
                return None, "temporarily lost"
            else:
                self.track = self.smooth_box = self.weak_since = None
                self.motion_steps = []
                self.motion_velocity = self.last_raw_center = None
                self.pending = None

        if selected is None and valid:
            center_box = (width / 2, height / 2, 0, 0)
            diagonal = math.hypot(width, height)
            best = max(valid, key=lambda c: c["confidence"] -
                       0.35 * center_distance(c["box"], center_box) / diagonal)
            if (self.pending is None or overlap(best["box"], self.pending) < 0.30):
                self.pending, self.pending_since = best["box"], now
            elif now - self.pending_since >= self.config.confirm_seconds:
                selected = best
            self.pending = best["box"]
        elif selected is None:
            self.pending = None

        if selected is None:
            self.last_update = now
            return None, "confirming" if self.pending is not None else "no target"

        raw_box = selected["box"]
        box = np.array(raw_box, dtype=float)
        if fragment_match:
            previous = np.array(self.track, dtype=float)
            dx = box[0] + box[2] / 2 - previous[0] - previous[2] / 2
            dx = np.clip(dx, -max(12, previous[2] * 0.5), max(12, previous[2] * 0.5))
            # A surviving fragment may be the torso or arm, so its vertical
            # midpoint is not a reliable replacement for the player's center.
            box = previous + (dx, 0, 0, 0)
        dt = now - self.last_seen
        if fragment_match or self.smooth_box is None or dt > self.config.lost_seconds:
            self.smooth_box = box
        else:
            # Large, credible motion responds immediately; small jitter is filtered.
            motion = center_distance(box, self.smooth_box)
            tau = self.config.smooth_seconds
            alpha = 1.0 if motion > max(8, box[3] * 0.12) else 1 - math.exp(-max(dt, 0) / max(tau, 1e-6))
            self.smooth_box += alpha * (box - self.smooth_box)
        self.track = tuple(int(round(v)) for v in box) if fragment_match else raw_box
        self.last_seen = self.last_update = now
        if weak_match or fragment_match:
            self.weak_since = now if self.weak_since is None else self.weak_since
        else:
            self.weak_since = None
        if weak_match or fragment_match:
            self.motion_steps = []
            self.motion_velocity = None
            self.last_raw_center = None
        else:
            center = (raw_box[0] + raw_box[2] / 2, raw_box[1] + raw_box[3] / 2)
            if self.last_raw_center is not None and self.last_raw_time is not None:
                elapsed = now - self.last_raw_time
                if 0 < elapsed <= 0.05:
                    step = ((center[0] - self.last_raw_center[0]) / elapsed,
                            (center[1] - self.last_raw_center[1]) / elapsed)
                    self.motion_steps = (self.motion_steps + [step])[-2:]
                else:
                    self.motion_steps = []
            if len(self.motion_steps) == 2:
                a, b = self.motion_steps
                lengths = math.hypot(*a), math.hypot(*b)
                agreement = (a[0] * b[0] + a[1] * b[1]) / max(1, lengths[0] * lengths[1])
                self.motion_velocity = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) if (
                    min(lengths) > 150 and agreement > 0.7) else None
            else:
                self.motion_velocity = None
            self.last_raw_center, self.last_raw_time = center, now
        self.pending = None
        result = dict(selected)
        result["raw_box"] = raw_box
        result["box"] = tuple(int(round(v)) for v in self.smooth_box)
        return result, ("tracking fragment" if fragment_match else
                        "tracking weak" if weak_match else "tracking")

    def _detect_region(self, frame, region, debug):
        x1, y1, x2, y2 = region
        roi = frame[y1:y2, x1:x2]
        raw = cv2.inRange(cv2.cvtColor(roi, cv2.COLOR_BGR2HSV), self.lower, self.upper)
        closed = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, self.close_kernel)
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        components = []
        for contour in contours:
            box = cv2.boundingRect(contour)
            x, y, w, h = box
            if w * h < self.config.min_component_pixels:
                continue
            if cv2.countNonZero(raw[y:y + h, x:x + w]) >= self.config.min_component_pixels:
                components.append(box)
        # Bound grouping cost on purple-heavy backgrounds; report truncation in debug.
        components.sort(key=lambda b: b[2] * b[3], reverse=True)
        truncated = len(components) > self.config.max_components
        components = components[:self.config.max_components]
        groups = self._groups(components)
        candidates = [self._candidate(box, count, raw, x1, y1) for box, count in groups]
        for candidate in candidates:
            x, y, w, h = candidate["box"]
            if x <= x1 or y <= y1 or x + w >= x2 or y + h >= y2:
                candidate["confidence"] *= 0.5
                candidate["reason"] = "clipped by search"
        return candidates, truncated, raw if debug else None, closed if debug else None

    def process(self, frame, now, debug=False):
        height, width = frame.shape[:2]
        if self.shape != (height, width):
            self.reset()
            self.shape = (height, width)
        region, wide = self._region(width, height, now)
        search_retry = False
        candidates, truncated, raw, closed = self._detect_region(frame, region, debug)
        if not wide and not any(
                c["reason"] in ("accepted", "fragment", "no shoulder shape", "low confidence") and
                c["box"][3] >= self.track[3] * 0.8 and
                c["box"][2] >= self.track[2] * 0.75 and
                center_distance(c["box"], self.track) < max(192, self.track[3] * 1.3)
                for c in candidates):
            # A fast pan can move the player outside the local crop. Search the
            # full area in this frame, before declaring the track missing.
            fraction = self.config.search_fraction
            mx, my = int(width * (1 - fraction) / 2), int(height * (1 - fraction) / 2)
            region = (mx, my, width - mx, height - my)
            candidates, truncated, raw, closed = self._detect_region(frame, region, debug)
            wide = True
            search_retry = True
            self.last_wide = now
        selected, status = self._select(candidates, now, width, height)
        credible = [c for c in candidates if c["reason"] == "accepted"]
        best_candidate = max(credible, key=lambda c: c["confidence"], default=None)
        target = None
        if selected:
            x, y, w, h = selected["box"]
            target = (int(x + w * 0.5), int(y + h * self.config.head_y_fraction))
        return {"best_box": selected["box"] if selected else None,
                "raw_box": selected["raw_box"] if selected else None,
                "motion_velocity": self.motion_velocity if selected else None,
                "best_candidate_box": best_candidate["box"] if best_candidate else None,
                "search_retry": search_retry,
                "confidence": selected["confidence"] if selected else 0.0,
                "target": target, "candidates": candidates, "status": status,
                "region": region, "wide_search": wide, "truncated": truncated,
                "raw_mask": raw if debug else None,
                "clean_mask": closed if debug else None}


def debug_image(frame, result):
    """Three fixed-width diagnostic panels, generated only at debug refresh rate."""
    x1, y1, x2, y2 = result["region"]
    preview = cv2.cvtColor(cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    for index, candidate in enumerate(result["candidates"]):
        x, y, w, h = candidate["box"]
        color = (0, 200, 255) if candidate["reason"] == "accepted" else (70, 70, 230)
        cv2.rectangle(preview, (x - x1, y - y1), (x + w - x1, y + h - y1), color, 1)
        label = f"#{index + 1}"
        cv2.putText(preview, label, (x - x1, max(12, y - y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
    if result["best_box"]:
        x, y, w, h = result["best_box"]
        cv2.rectangle(preview, (x - x1, y - y1), (x + w - x1, y + h - y1), (0, 255, 0), 2)
    panels = []
    for title, source in (("Raw purple mask", result["raw_mask"]),
                          ("Gentle closing", result["clean_mask"]),
                          ("Candidates / selected green", preview)):
        if source.ndim == 2:
            source = cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
        scale = min(480 / source.shape[1], 270 / source.shape[0])
        resized = cv2.resize(source, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST)
        if source is not preview and scale < 1:
            # Preserve visibility of one-pixel mask strokes in the small preview.
            # This changes display brightness only, never the detector's masks.
            resized = np.minimum(resized.astype(np.float32) / scale, 255).astype(np.uint8)
        panel = np.zeros((390, 480, 3), np.uint8)
        panel[30:30 + resized.shape[0], :resized.shape[1]] = resized
        cv2.putText(panel, title, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        if source is preview:
            for index, candidate in enumerate(result["candidates"][:5]):
                label = (f"#{index + 1}  {candidate['confidence']:.2f}  "
                         f"{candidate['reason']}  parts={candidate['parts']}")
                cv2.putText(panel, label, (8, 316 + index * 16), cv2.FONT_HERSHEY_SIMPLEX,
                            0.43, (220, 220, 220), 1)
        else:
            details = ("Threshold only; thin strokes preserved" if title.startswith("Raw")
                       else "3x3 closing; component filtering follows")
            cv2.putText(panel, details, (8, 320), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (220, 220, 220), 1)
            if result["truncated"]:
                cv2.putText(panel, "Component cap reached: small regions omitted", (8, 343),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.43, (0, 200, 255), 1)
        panels.append(panel)
    return np.hstack(panels)
