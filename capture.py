"""
Frame capture — extract a JPEG from the ring buffer (or fall back to live RTSP).

The ring buffer (ring_buffer.py) continuously records the camera's RTSP stream
to short segment files in /tmp. This module extracts a single JPEG frame from
the segment that was being written at a specified time in the past.
"""

import logging
import subprocess
import tempfile
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

    # The newest segment is actively being written by ffmpeg — reading it
    # risks incomplete frames (bottom-of-image smearing). Exclude it.
    if len(segments) >= 2:
        active_seg = segments[-1]
        candidates = segments[:-1]
        log.debug(f"Skipping active segment {active_seg.name}")
    else:
        candidates = segments

    now = time.time()
    target_time = now - lookback_s

    # Walk newest-first to find the segment containing our target time.
    best = None
    best_idx = None
    seek_pos = 0.0

    for i, seg in enumerate(reversed(candidates)):
        seg_end = seg.stat().st_mtime
        seg_start = seg_end - segment_seconds
        if seg_start <= target_time <= seg_end:
            best = seg
            best_idx = len(candidates) - 1 - i
            seek_pos = max(0, target_time - seg_start)
            break

    if best is None:
        # Target time outside buffer — use newest completed segment
        best = candidates[-1] if candidates else segments[0]
        best_idx = len(candidates) - 1
        seek_pos = 0.0
        age = now - best.stat().st_mtime
        log.info(
            f"Lookback {lookback_s:.0f}s exceeds completed buffer; "
            f"using newest complete segment ({best.name}, {age:.0f}s old)"
        )

    # Prepend preceding segments so ffmpeg always sees a keyframe before our
    # target. H.264 P/B-frames decoded without their reference I-frame produce
    # the smeared/ghosted artifacts we used to see in the gallery. The Aqara
    # G5 Pro's GOP can be 2-4s (sometimes longer when bitrate is low), so a
    # single 2s segment isn't always self-contained. Feeding ~8s of footage
    # covers the realistic worst case while still finishing in <1s on the Pi.
    _PREPEND_MAX = 3
    feed_segments = []
    if best_idx is not None and best_idx > 0:
        start = max(0, best_idx - _PREPEND_MAX)
        prepended = candidates[start:best_idx]
        feed_segments.extend(prepended)
        seek_pos += len(prepended) * segment_seconds
    feed_segments.append(best)

    # If target_time lands at a segment boundary (common for monitor.py's
    # lookback_s=2 with 2s segments), seek_pos can equal the concat'd stream
    # duration exactly, and ffmpeg returns EINVAL (rc=234) on the seek. Pull
    # back by a small epsilon — 0.2s is enough headroom for a 5 fps stream
    # (one frame interval) while losing minimal timing accuracy at the
    # camera's actual 15-25 fps.
    estimated_stream_s = len(feed_segments) * segment_seconds
    seek_pos = max(0.0, min(seek_pos, estimated_stream_s - 0.2))

    concat_file = None
    try:
        if len(feed_segments) > 1:
            # Use ffmpeg concat demuxer to read both segments
            concat_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False,
            )
            for seg in feed_segments:
                concat_file.write(f"file '{seg}'\n")
            concat_file.close()

            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", concat_file.name,
            ]
            log.debug(f"Concat: {[s.name for s in feed_segments]}")
        else:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(best),
            ]

        # Output seeking (-ss after -i): ffmpeg decodes from the start,
        # ensuring it hits a keyframe before our target frame.
        if seek_pos > 0.3:
            cmd += ["-ss", f"{seek_pos:.1f}"]
        cmd += [
            "-vframes", "1",
            "-q:v", str(quality),
            "-f", "image2pipe",
            "-c:v", "mjpeg",
            "pipe:1",
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode == 0 and len(result.stdout) > 1000:
            log.info(
                f"Extracted frame: {best.name} "
                f"(lookback={lookback_s:.0f}s, seek={seek_pos:.1f}s, "
                f"segs={len(feed_segments)}, {len(result.stdout):,} bytes)"
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
    finally:
        if concat_file:
            Path(concat_file.name).unlink(missing_ok=True)

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
    # -skip_frame nonkey tells the input decoder to discard everything that
    # isn't an I-frame, so the first frame ffmpeg emits is guaranteed to be
    # a self-contained keyframe. Without this, the very first decoded frame
    # off a freshly-opened RTSP stream is often a P/B-frame whose reference
    # I-frame hasn't arrived yet — that's the smeared/ghosted output we used
    # to see in the gallery. Positioned before -i so it configures the
    # decoder rather than the MJPEG encoder on the output side.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-rtsp_transport", transport,
        "-skip_frame", "nonkey",
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
