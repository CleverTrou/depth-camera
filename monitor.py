#!/usr/bin/env python3
"""
Local Motion Monitor — RTSP frame-differencing trigger.

Fallback for when IFTTT/Aqara cloud is unavailable. Detects pixel-level
changes in the camera stream and triggers the depth pipeline locally.

This is less smart than the Aqara AI (can't tell a person from a swaying
tree), but works fully offline.

Usage:
    python3 monitor.py
    python3 monitor.py --config config.yaml
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import yaml

from capture import extract_frame, capture_direct
from pipeline import process_event

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "camera": {
        "rtsp_url": "rtsp://USER:PASS@CAMERA_IP:8554/stream_path",
        "rtsp_transport": "tcp",
    },
    "ring_buffer": {
        "dir": "/tmp/depth-ring",
        "segment_seconds": 2,
    },
    "detection": {
        "poll_interval": 3,
        "compare_width": 320,
        "compare_height": 240,
        "threshold": 20,
        "min_changed_pct": 5.0,
        # 30s was producing ~90 triggers/hour on a windy day (sustained motion
        # saturates the cooldown). 120s caps it at ~30/hour while we tune the
        # threshold + min_changed_pct from real telemetry.
        "cooldown": 120,
        "confirm_frames": 2,
        "lookback_s": 2,
        "snapshot_quality": 2,
    },
    "pipeline": {
        "data_dir": "/data/depth-camera",
        "max_events": 200,
        "ply_downsample": 2,
        "depth_input_size": 518,
        "colormap": "inferno",
    },
    "notifications": {
        "ntfy_topic_url": "",
    },
}


def load_config(path):
    config = DEFAULT_CONFIG.copy()
    if path and Path(path).exists():
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        _deep_merge(config, user)
    if os.environ.get("CAMERA_RTSP_URL"):
        config["camera"]["rtsp_url"] = os.environ["CAMERA_RTSP_URL"]
    if os.environ.get("NTFY_TOPIC_ALERTS"):
        config["notifications"]["ntfy_topic_url"] = os.environ["NTFY_TOPIC_ALERTS"]
    return config


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# Frame capture and comparison
# ---------------------------------------------------------------------------


def capture_raw_frame(rtsp_url, transport, width, height):
    """Capture a low-res raw RGB frame for motion comparison."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-rtsp_transport", transport,
        "-i", rtsp_url,
        "-vframes", "1",
        "-vf", f"scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode != 0:
            return None
        expected = width * height * 3
        if len(result.stdout) < expected:
            return None
        return result.stdout[:expected]
    except (subprocess.TimeoutExpired, Exception):
        return None


def save_motion_diff(
    reference: bytes,
    current: bytes,
    width: int,
    height: int,
    threshold: int,
    event_dir: Path,
) -> None:
    """Save a motion diff JPEG: dimmed current frame with motion pixels highlighted orange."""
    try:
        from PIL import Image

        expected = width * height * 3
        if len(reference) < expected or len(current) < expected:
            return

        ref = np.frombuffer(reference[:expected], dtype=np.uint8).reshape(height, width, 3).astype(float)
        cur = np.frombuffer(current[:expected], dtype=np.uint8).reshape(height, width, 3).astype(float)

        ref_lum = ref.mean(axis=2)
        cur_lum = cur.mean(axis=2)
        diffs = cur_lum - ref_lum
        motion_mask = np.abs(diffs - float(diffs.mean())) > threshold

        vis = cur * 0.45
        vis[motion_mask, 0] = 255
        vis[motion_mask, 1] = 140
        vis[motion_mask, 2] = 0

        img = Image.fromarray(vis.clip(0, 255).astype(np.uint8))
        img = img.resize((width * 3, height * 3), Image.NEAREST)
        img.save(event_dir / "motion_diff.jpg", quality=85)
    except Exception as e:
        log.warning(f"Failed to save motion diff: {e}")


def compute_frame_diff(frame_a, frame_b, threshold):
    """Compare two raw RGB frames. Returns (mean_diff, pct_changed).

    Brightness-normalised: subtracts the per-frame mean luminance before
    comparing so global auto-exposure shifts don't register as motion.
    mean_diff is the raw (un-normalised) mean absolute diff for metadata.
    """
    if len(frame_a) != len(frame_b):
        return 0.0, 0.0

    a = np.frombuffer(frame_a, dtype=np.uint8).reshape(-1, 3).mean(axis=1)
    b = np.frombuffer(frame_b, dtype=np.uint8).reshape(-1, 3).mean(axis=1)

    diffs = a - b
    total_abs_mean = float(np.abs(diffs).mean())

    # mean_signed captures global brightness shift (auto-exposure, sunrise, etc.)
    mean_signed = float(diffs.mean())
    changed_count = int(np.sum(np.abs(diffs - mean_signed) > threshold))

    return total_abs_mean, (changed_count / len(a)) * 100


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

running = True


def handle_signal(signum, _):
    global running
    running = False


def main():
    parser = argparse.ArgumentParser(description="Motion monitor")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    config = load_config(args.config)
    cam = config["camera"]
    det = config["detection"]
    ring = config["ring_buffer"]
    pipe = config["pipeline"]
    ntfy_url = config.get("notifications", {}).get("ntfy_topic_url") or None

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info("=" * 55)
    log.info("Depth Camera — Motion Monitor")
    log.info(f"  Camera:   {cam['rtsp_url'][:40]}...")
    log.info(f"  Poll:     every {det['poll_interval']}s")
    log.info(f"  Confirm:  {det['confirm_frames']} consecutive frames")
    log.info(f"  Lookback: {det['lookback_s']}s into ring buffer")
    log.info("=" * 55)

    # Pre-load depth model
    from depth import load_model
    load_model()

    # Capture reference frame
    log.info("Capturing reference frame...")
    reference = None
    while reference is None and running:
        reference = capture_raw_frame(
            cam["rtsp_url"], cam["rtsp_transport"],
            det["compare_width"], det["compare_height"],
        )
        if reference is None:
            log.warning("Failed — retrying in 5s...")
            time.sleep(5)

    if not running:
        return

    log.info("Reference captured. Monitoring...")

    last_trigger = 0.0
    confirm_count = 0
    pct_window: list[tuple[float, float]] = []  # (pct, mean_diff) per poll
    samples_per_minute = max(1, int(60 / det["poll_interval"]))
    min_mean_diff = det.get("min_mean_diff", 0)

    while running:
        time.sleep(det["poll_interval"])

        now = time.time()

        current = capture_raw_frame(
            cam["rtsp_url"], cam["rtsp_transport"],
            det["compare_width"], det["compare_height"],
        )
        if current is None:
            confirm_count = 0
            continue

        mean_diff, pct = compute_frame_diff(reference, current, det["threshold"])
        in_cooldown = (now - last_trigger) < det["cooldown"]

        # Rolling minute summary so we can see the noise floor without
        # spamming a log line for every poll.
        pct_window.append((pct, mean_diff))
        if len(pct_window) >= samples_per_minute:
            pcts = sorted(p for p, _ in pct_window)
            n = len(pcts)
            p50 = pcts[n // 2]
            p90 = pcts[min(n - 1, int(n * 0.9))]
            over = sum(
                1 for p, m in pct_window
                if p >= det["min_changed_pct"] and m >= min_mean_diff
            )
            log.info(
                f"baseline ({n} polls): "
                f"max={pcts[-1]:.1f}% p90={p90:.1f}% p50={p50:.1f}% "
                f"over-threshold={over}/{n}"
            )
            pct_window = []

        if in_cooldown:
            # Adapt the reference during cooldown so it reflects the current
            # scene by the time we're ready to detect again. Without this, a
            # stale reference (e.g. captured at a different time of day) causes
            # every poll to appear as motion and the monitor fires on every
            # cooldown expiry instead of on actual events.
            reference = current
            confirm_count = 0
        elif pct >= det["min_changed_pct"] and mean_diff >= min_mean_diff:
            confirm_count += 1
            if confirm_count >= det["confirm_frames"]:
                dt_since_last = now - last_trigger if last_trigger else -1
                log.info(
                    f"MOTION CONFIRMED: pct={pct:.1f}% "
                    f"mean_diff={mean_diff:.1f} "
                    f"streak={confirm_count} polls "
                    f"dt_since_last={dt_since_last:.0f}s — running depth pipeline"
                )

                image_data = extract_frame(
                    ring["dir"], det["lookback_s"],
                    ring["segment_seconds"], det["snapshot_quality"],
                )
                if image_data is None:
                    image_data = capture_direct(
                        cam["rtsp_url"], cam["rtsp_transport"],
                        det["snapshot_quality"],
                    )

                if image_data:
                    result = process_event(
                        image_data,
                        data_dir=pipe["data_dir"],
                        source="pi_monitor",
                        event_type="motion",
                        max_events=pipe["max_events"],
                        ply_downsample=pipe["ply_downsample"],
                        depth_input_size=pipe["depth_input_size"],
                        colormap=pipe["colormap"],
                        ntfy_topic_url=ntfy_url,
                        extra_metadata={
                            "trigger_pct": round(pct, 2),
                            "trigger_mean_diff": round(mean_diff, 2),
                            "trigger_confirm_streak": confirm_count,
                            "trigger_dt_since_last_s": round(dt_since_last, 1),
                            "detection_threshold": det["threshold"],
                            "detection_min_changed_pct": det["min_changed_pct"],
                            "compare_width": det["compare_width"],
                            "compare_height": det["compare_height"],
                        },
                    )
                    if result:
                        log.info(f"Event {result['event_id']} processed in {result['elapsed_s']}s")
                        event_dir = Path(pipe["data_dir"]) / "events" / result["event_id"]
                        save_motion_diff(
                            reference, current,
                            det["compare_width"], det["compare_height"],
                            det["threshold"], event_dir,
                        )

                last_trigger = now
                confirm_count = 0
        else:
            confirm_count = 0
            reference = current

    log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
