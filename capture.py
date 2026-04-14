"""
Frame capture — extract a JPEG from the ring buffer (or fall back to live RTSP).

The ring buffer (ring_buffer.py) continuously records the camera's RTSP stream
to short segment files in /tmp. This module extracts a single JPEG frame from
the segment that was being written at a specified time in the past.
"""

import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger("capture")


def extract_frame(
    ring_dir: str,
    lookback_s: float = 5.0,
    segment_seconds: int = 2,
    quality: int = 2,
) -> bytes | None:
    """
    Extract a JPEG frame from the ring buffer at a past timestamp.

    Uses segment file modification times to find the right one, then
    decodes a single frame from it via ffmpeg.

    Args:
        ring_dir: Path to ring buffer directory (e.g. /tmp/depth-ring).
        lookback_s: Seconds into the past to grab the frame.
        segment_seconds: Configured segment duration.
        quality: ffmpeg -q:v JPEG quality (1=best, 31=worst).

    Returns:
        JPEG bytes, or None if extraction failed.
    """
    ring_path = Path(ring_dir)
    segments = sorted(ring_path.glob("seg_*.ts"), key=lambda p: p.stat().st_mtime)

    if not segments:
        log.warning("Ring buffer empty — no segments found")
        return None

    now = time.time()
    target_time = now - lookback_s

    # Walk newest-first to find the segment containing our target time.
    best = None
    seek_pos = 0.0

    for seg in reversed(segments):
        seg_end = seg.stat().st_mtime
        seg_start = seg_end - segment_seconds
        if seg_start <= target_time <= seg_end:
            best = seg
            seek_pos = max(0, target_time - seg_start)
            break

    if best is None:
        # Target time outside buffer — use oldest available
        best = segments[0]
        seek_pos = 0.0
        age = now - best.stat().st_mtime
        log.info(
            f"Lookback {lookback_s:.0f}s exceeds buffer; "
            f"using oldest segment ({best.name}, {age:.0f}s old)"
        )

    # Extract one JPEG frame via ffmpeg.
    # Place -ss AFTER -i (output seeking) so ffmpeg decodes from the segment
    # start and lands on a clean frame. With 2s segments this is near-instant.
    # Input seeking (-ss before -i) causes smeared frames because it lands on
    # P-frames without their reference I-frame.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(best),
    ]
    if seek_pos > 0.3:
        cmd += ["-ss", f"{seek_pos:.1f}"]
    cmd += [
        "-vframes", "1",
        "-q:v", str(quality),
        "-f", "image2pipe",
        "-c:v", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode == 0 and len(result.stdout) > 1000:
            log.info(
                f"Extracted frame: {best.name} "
                f"(lookback={lookback_s:.0f}s, seek={seek_pos:.1f}s, "
                f"{len(result.stdout):,} bytes)"
            )
            return result.stdout
        log.warning(
            f"ffmpeg extraction failed (rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace')[-300:]}"
        )
    except subprocess.TimeoutExpired:
        log.warning("Frame extraction timed out")
    except Exception as e:
        log.error(f"Frame extraction error: {e}")

    return None


def capture_direct(
    rtsp_url: str,
    transport: str = "tcp",
    quality: int = 2,
) -> bytes | None:
    """
    Fallback: capture a frame directly from the live RTSP stream.

    This grabs whatever the camera shows NOW — not from the past.
    Only used when the ring buffer is unavailable.
    """
    log.info("Attempting direct RTSP capture (fallback — live frame)...")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-rtsp_transport", transport,
        "-i", rtsp_url,
        "-vframes", "1",
        "-q:v", str(quality),
        "-f", "image2pipe",
        "-c:v", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode == 0 and len(result.stdout) > 1000:
            log.info(f"Direct RTSP capture: {len(result.stdout):,} bytes")
            return result.stdout
        log.warning(f"Direct capture failed (rc={result.returncode})")
    except subprocess.TimeoutExpired:
        log.warning("Direct RTSP capture timed out")
    except Exception as e:
        log.error(f"Direct capture error: {e}")

    return None
