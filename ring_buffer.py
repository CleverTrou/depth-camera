#!/usr/bin/env python3
"""
Ring Buffer Daemon — continuous RTSP recording with circular segments.

Maintains a rolling window of video from the camera's RTSP stream using
ffmpeg's segment muxer with -c copy (near-zero CPU — just remuxes H.264).

This ensures we always have the last ~16 seconds of footage. When a
detection event arrives (3-10s after it happened), other components
extract the right frame from the buffer.

Resource usage:
    CPU:  ~0% (copy codec, no transcoding)
    RAM:  ~8-16 MB in /tmp (tmpfs)
    Net:  one persistent RTSP connection

Usage:
    python3 ring_buffer.py
    python3 ring_buffer.py --config config.yaml
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "camera": {
        "rtsp_url": "rtsp://USER:PASS@192.168.1.100:8554/1520p",
        "rtsp_transport": "tcp",
    },
    "ring_buffer": {
        "dir": "/tmp/depth-ring",
        "segment_seconds": 2,
        "segment_count": 8,
        "stale_timeout": 15,
        "restart_delay": 3,
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
    return config


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ring-buffer")

# ---------------------------------------------------------------------------
# ffmpeg management
# ---------------------------------------------------------------------------

running = True


def handle_signal(signum, _frame):
    global running
    log.info(f"Signal {signum} — shutting down")
    running = False


def build_ffmpeg_cmd(config):
    cam = config["camera"]
    ring = config["ring_buffer"]
    return [
        "ffmpeg",
        "-loglevel", "warning",
        "-rtsp_transport", cam["rtsp_transport"],
        "-i", cam["rtsp_url"],
        "-c", "copy",
        "-an",
        "-f", "segment",
        "-segment_time", str(ring["segment_seconds"]),
        "-segment_wrap", str(ring["segment_count"]),
        "-reset_timestamps", "1",
        str(Path(ring["dir"]) / "seg_%03d.ts"),
    ]


def ring_is_healthy(ring_dir, stale_timeout):
    segments = list(Path(ring_dir).glob("seg_*.ts"))
    if not segments:
        return False
    newest_mtime = max(seg.stat().st_mtime for seg in segments)
    return (time.time() - newest_mtime) < stale_timeout


def stop_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global running

    parser = argparse.ArgumentParser(description="RTSP ring buffer daemon")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    config = load_config(args.config)
    ring = config["ring_buffer"]
    buffer_seconds = ring["segment_count"] * ring["segment_seconds"]

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    Path(ring["dir"]).mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_cmd(config)

    log.info("=" * 50)
    log.info("Depth Camera — Ring Buffer")
    log.info(f"  Camera:   {config['camera']['rtsp_url'][:40]}...")
    log.info(f"  Ring dir: {ring['dir']}")
    log.info(f"  Buffer:   {ring['segment_count']} x {ring['segment_seconds']}s "
             f"= {buffer_seconds}s")
    log.info("=" * 50)

    proc = None

    while running:
        log.info("Starting ffmpeg ring buffer...")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            log.error("ffmpeg not found — install: sudo apt install ffmpeg")
            sys.exit(1)

        log.info(f"ffmpeg started (pid={proc.pid})")
        startup_grace = True

        while running:
            time.sleep(5)

            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace")[-500:] if proc.stderr else ""
                log.warning(f"ffmpeg exited (code={proc.poll()}): {stderr}")
                break

            if ring_is_healthy(ring["dir"], ring["stale_timeout"]):
                if startup_grace:
                    log.info("Ring buffer active — segments flowing")
                    startup_grace = False
            elif not startup_grace:
                log.warning("Ring buffer stale — restarting ffmpeg")
                stop_process(proc)
                break

        if proc is not None:
            stop_process(proc)

        if running:
            log.info(f"Restarting in {ring['restart_delay']}s...")
            time.sleep(ring["restart_delay"])

    log.info("Ring buffer stopped.")


if __name__ == "__main__":
    main()
