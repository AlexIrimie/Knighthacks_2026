# Knighthacks 2026

Purple-outline player detection POC for the practice range. Capture and detection
are capped at **120 FPS**; actual throughput depends on processing and capture time.

```powershell
pip install dxcam opencv-python numpy PyQt6
python valorant_live_overlay_poc.py
```

Use Borderless / Windowed Fullscreen on the primary monitor with purple enemy
outlines. Press Ctrl+C in the terminal to stop. The overlay shows the selected
box, an approximate head point, confidence, detection time, processing FPS and
time since the frame reached Python. The head point is still a box-based estimate.
The center reticle and center-to-target line have been removed.

## Inspect detections

```powershell
python valorant_live_overlay_poc.py --debug
```

The diagnostic window shows the raw purple mask, gently closed mask, and all
grouped candidates. Yellow boxes pass the shape checks; red boxes show rejection
reasons; green is the selected, lightly smoothed box. Labels show confidence and
the number of grouped outline components. Confidence is a heuristic, not a
probability. Debug refreshes at 15 FPS and adds processing overhead; leave it off
when measuring normal latency. Windows capture exclusion is requested for both
windows. If unavailable, a warning tells you to move diagnostics to another monitor.

## Record and compare the same frames

With the range ready, start a short recording and return to the game:

```powershell
python valorant_live_overlay_poc.py --record-seconds 10
python valorant_live_overlay_poc.py --replay captures/SESSION_FOLDER --debug
```

Recordings contain lossless PNGs and a timestamp manifest under `captures/`.
Sampling defaults to 30 FPS to limit disk overhead. Use `--record-fps 120` for
denser temporal samples; a bounded background writer drops samples if it cannot
keep up and reports the count on exit. Recording runs for the requested duration
from capture startup, while detection continues until stopped. Replay uses the
original sample timestamps, writes a metrics CSV and, with `--debug`, diagnostic
PNGs in the recording's `debug/` folder. Lower-rate recordings cannot reproduce
every tracking decision of a 120 FPS run.

Metrics default to unique files under `runs/`, preserving previous logs. Use
`--csv path.csv` to choose a destination (overwrites that path).
`--no-csv` skips live per-frame writes for performance comparison; the destination
contains only a header. It does not suppress replay results.

## What changed

- Thin outlines survive: no morphological opening or broad dilation. A cached
  3×3 closing kernel bridges small gaps, and tiny components are discarded.
- Nearby fragments are grouped conservatively, with size and alignment limits
  and a guard against joining two substantial adjacent silhouettes.
- Candidates need vertical coverage, plausible proportions, horizontal outline
  span and widening from the head region toward the shoulders. Smaller distant
  outlines use one-pixel shoulder evidence and looser size/coverage thresholds.
  An isolated arm-shaped blob is treated as uncertain.
- The tracker favors overlap, position and size continuity. It suppresses sudden
  collapse to a smaller fragment, briefly retains identity through missing frames,
  and publishes no target while detection is missing. A full-size outline whose
  shape briefly degrades can retain the track for up to 50 ms (`tracking weak`);
  a smaller arm cannot use this path. New candidates require
  about 16 ms of consistent evidence (roughly three frames at 120 FPS).
- Small jitter is filtered with a 12 ms time constant; larger credible movement
  updates immediately. This filter and initial confirmation intentionally add a
  small delay in exchange for stability.
- Tracking searches a generously padded local region, with a wider search every
  200 ms. If the local search has no plausible match, it retries the wide region
  in the same frame. Kernel allocations are outside the frame loop.
- Direct DXCam grabs process new desktop updates with no application frame queue;
  a monotonic timer limits capture attempts to 120 per second. Detection slows
  down if a frame takes longer than the 8.33 ms budget.

The loop requests 1 ms Windows timer resolution while running and restores it on
exit, following Microsoft's [timer API guidance](https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod).
The diagnostic windows use Windows [capture exclusion](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity).

## Tune and measure

Detection defaults live in `DetectionConfig` in `player_detection.py`; the live
instance is `CONFIG` in `detection_app.py`. Start with the HSV limits only after
checking the raw mask. Tune shape/grouping rules against recorded failures, not
just the selected box. The search covers the central 90% of the frame by default.

`vision_processing_ms` includes masking, grouping, selection and smoothing.
`capture_call_ms` measures the DXCam call, `publish_ms` the shared-state update,
and `previous_csv_write_ms` the previous frame's CSV write/flush. `debug_ms` and
`record_submit_ms` help identify optional overhead. `rejection_reasons` lists
why candidate blobs failed shape checks. Overlay `age` starts when
Python receives the frame. None of these measure game-render-to-monitor latency
or GPU presentation delay. FPS counts received desktop frames, not necessarily
unique game-rendered frames.

These are color/shape heuristics, not a trained player recognizer. Occlusion,
unusual poses, overlapping players and purple effects can still cause misses or
false detections. The shoulder check deliberately withholds uncertain partial
bodies. The 48-component cap bounds grouping work in noisy scenes but may discard
small candidates; `components_truncated` flags that condition in the CSV.
Monitor output 0 is currently assumed to correspond to the primary overlay screen.

## Verification

```powershell
python -m unittest -v test_player_detection.py
```

Synthetic tests cover thin outlines, fragment grouping, adjacent players, isolated
arms, track collapse, target continuity, detection loss, jitter, resolution changes
and recording/replay. Real practice-range footage is still needed to validate
accuracy and end-to-end latency. The original POC is preserved as
`valorant_live_overlay_poc.py.bak`; the existing metrics CSV is untouched.
