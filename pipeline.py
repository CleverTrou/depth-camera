"""
Processing pipeline — orchestrates capture → depth → outputs.

This is the shared "brain" used by both relay.py and monitor.py.
Given JPEG bytes from a trigger event, it:
  1. Saves the original snapshot
  2. Runs depth estimation (~3-6s on Pi 5 CPU)
  3. Generates all output formats (colormap, depth image, point cloud)
  4. Returns the event ID and output paths

Events are stored in the data directory:
    /data/depth-camera/events/
        20260410_143022_abc123/
            snapshot.jpg          # Original RGB capture
            depth.npy             # Raw depth array (float32, 0-1)
            depth_colormap.jpg    # Colorized visualization
            depth_map.png         # 16-bit grayscale for parallax viewer
            pointcloud.ply        # Colored point cloud
"""

import io
import json
import logging
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from depth import estimate_depth, load_model
from outputs import generate_colormap, generate_depth_image, generate_pointcloud

log = logging.getLogger("pipeline")

_lock = threading.Lock()
_is_processing = False


def is_processing() -> bool:
    return _is_processing


def make_event_id() -> str:
    """Generate a timestamped event ID."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"{ts}_{short}"


def process_event(
    image_data: bytes,
    data_dir: str,
    source: str = "unknown",
    event_type: str = "detection",
    max_events: int = 200,
    ply_downsample: int = 2,
    depth_input_size: int = 518,
    colormap: str = "inferno",
) -> dict | None:
    """
    Run the full processing pipeline on a captured JPEG frame.

    Args:
        image_data: JPEG bytes of the captured frame.
        data_dir: Root data directory (e.g. /data/depth-camera).
        source: Where the trigger came from (ifttt, pi_monitor, test).
        event_type: What was detected (person, animal, motion).
        max_events: Maximum events to keep on disk.
        ply_downsample: Point cloud downsampling factor (2 = quarter pixels).
        depth_input_size: Model input resolution (518 default, 384 for speed).
        colormap: Matplotlib colormap for depth visualization.

    Returns:
        Dict with event_id and output paths, or None if processing failed.
    """
    global _is_processing

    with _lock:
        if _is_processing:
            log.warning("Skipping — already processing another event")
            return None
        _is_processing = True

    event_id = make_event_id()

    try:
        events_dir = Path(data_dir) / "events"
        event_dir = events_dir / event_id
        event_dir.mkdir(parents=True, exist_ok=True)

        log.info(f"[{event_id}] Processing event (source={source}, type={event_type})")
        t_start = time.time()

        # Step 1: Save original snapshot
        snapshot_path = event_dir / "snapshot.jpg"
        snapshot_path.write_bytes(image_data)
        log.info(f"[{event_id}] Saved snapshot ({len(image_data):,} bytes)")

        # Load image as numpy array
        pil_image = Image.open(io.BytesIO(image_data)).convert("RGB")
        rgb = np.array(pil_image)

        # Step 2: Depth estimation
        t_depth = time.time()
        session = load_model()
        depth_map = estimate_depth(rgb, session, model_input_size=depth_input_size)
        log.info(
            f"[{event_id}] Depth estimated in {time.time() - t_depth:.1f}s "
            f"(input={depth_input_size}, output={depth_map.shape})"
        )

        # Save raw depth for later use
        np.save(str(event_dir / "depth.npy"), depth_map)

        # Step 3: Generate outputs
        generate_colormap(depth_map, event_dir / "depth_colormap.jpg", colormap)
        generate_depth_image(depth_map, event_dir / "depth_map.png")
        generate_pointcloud(rgb, depth_map, event_dir / "pointcloud.ply", ply_downsample)

        elapsed = time.time() - t_start
        log.info(f"[{event_id}] Pipeline complete in {elapsed:.1f}s")

        # Step 4: Save metadata
        metadata = {
            "event_id": event_id,
            "source": source,
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
            "elapsed_s": round(elapsed, 1),
            "depth_input_size": depth_input_size,
            "ply_downsample": ply_downsample,
            "image_size": list(pil_image.size),
        }
        (event_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

        # Step 5: Housekeeping — remove oldest events
        _cleanup_old_events(events_dir, max_events)

        return metadata

    except Exception as e:
        log.error(f"[{event_id}] Pipeline failed: {e}", exc_info=True)
        return None
    finally:
        with _lock:
            _is_processing = False


def _cleanup_old_events(events_dir: Path, max_events: int):
    """Remove oldest events beyond the configured maximum."""
    all_events = sorted(
        [d for d in events_dir.iterdir() if d.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    for old_dir in all_events[max_events:]:
        shutil.rmtree(old_dir, ignore_errors=True)
        log.info(f"Cleaned up old event: {old_dir.name}")
