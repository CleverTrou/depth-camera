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
from notifications import push_ntfy
from outputs import generate_colormap, generate_depth_image, generate_pointcloud

log = logging.getLogger("pipeline")

_lock = threading.Lock()
_is_processing = False

# event_id -> source. Lets _cleanup_old_events skip metadata.json reads on
# warm runs; rebuilt lazily after a process restart. Bounded by total events
# on disk (max_events × number of sources).
_source_by_id: dict[str, str] = {}


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
    camera_hfov_deg: float = 113.0,
    ply_depth_scale: float = 2.5,
    ply_ground_correction: bool = True,
    ntfy_topic_url: str | None = None,
    extra_metadata: dict | None = None,
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
        extra_metadata: Optional dict merged into metadata.json. Lets callers
            persist trigger diagnostics (motion pct, mean_diff, etc.) without
            growing the function signature for every new field.

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
        generate_pointcloud(
            rgb, depth_map, event_dir / "pointcloud.ply",
            ply_downsample, depth_scale=ply_depth_scale,
            hfov_deg=camera_hfov_deg,
            ground_correction=ply_ground_correction,
        )

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
            "ply_depth_scale": ply_depth_scale,
            "image_size": list(pil_image.size),
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        (event_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

        # Step 5: Housekeeping — remove oldest events
        _source_by_id[event_id] = source
        _cleanup_old_events(events_dir, max_events)

        return metadata

    except Exception as e:
        log.error(f"[{event_id}] Pipeline failed: {e}", exc_info=True)
        push_ntfy(
            ntfy_topic_url,
            "Depth Camera: pipeline failed",
            f"Event {event_id} ({event_type} from {source}) — "
            f"{type(e).__name__}: {e}",
            priority="high",
            tags=["rotating_light"],
        )
        return None
    finally:
        with _lock:
            _is_processing = False


def _cleanup_old_events(events_dir: Path, max_events: int):
    """Remove oldest events beyond max_events, applied independently per source.

    Each trigger source (ifttt, pi_monitor, …) gets its own quota so a noisy
    fallback can't evict events from the curated IFTTT feed and vice versa.
    """
    event_dirs = [d for d in events_dir.iterdir() if d.is_dir()]
    by_source: dict[str, list[Path]] = {}
    for d in sorted(event_dirs, key=lambda p: p.name, reverse=True):
        source = _source_by_id.get(d.name)
        if source is None:
            source = "unknown"
            meta_file = d / "metadata.json"
            if meta_file.exists():
                try:
                    parsed = json.loads(meta_file.read_text())
                    if isinstance(parsed, dict):
                        source = parsed.get("source", "unknown")
                except (json.JSONDecodeError, OSError):
                    pass
            _source_by_id[d.name] = source
        by_source.setdefault(source, []).append(d)

    for source, dirs in by_source.items():
        for old_dir in dirs[max_events:]:
            shutil.rmtree(old_dir, ignore_errors=True)
            _source_by_id.pop(old_dir.name, None)
            log.info(f"Cleaned up old event ({source}): {old_dir.name}")
