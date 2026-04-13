"""
Output generators — turn RGB + depth map into visual formats.

All functions accept a normalized depth map where 1.0 = nearest, 0.0 = farthest.
"""

import logging
import struct
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("outputs")


# ---------------------------------------------------------------------------
# Colorized depth map
# ---------------------------------------------------------------------------


def generate_colormap(
    depth: np.ndarray,
    output_path: str | Path,
    colormap: str = "inferno",
) -> Path:
    """
    Save a colorized depth map image.

    Uses matplotlib's perceptual colormaps for clear near/far visualization.
    Warm colors = near, cool colors = far.

    Args:
        depth: Float32 array (H, W), normalized [0, 1], 1 = nearest.
        output_path: Where to save the JPEG.
        colormap: Matplotlib colormap name (inferno, viridis, magma, turbo).
    """
    output_path = Path(output_path)

    try:
        import matplotlib.cm as cm
        cmap = cm.get_cmap(colormap)
    except ImportError:
        # Fallback: simple grayscale if matplotlib isn't available
        gray = (depth * 255).astype(np.uint8)
        Image.fromarray(gray).save(str(output_path), quality=92)
        log.info(f"Colormap saved (grayscale fallback): {output_path.name}")
        return output_path

    # Apply colormap (expects values in [0, 1], returns RGBA float)
    colored = cmap(depth)
    colored_rgb = (colored[:, :, :3] * 255).astype(np.uint8)

    Image.fromarray(colored_rgb).save(str(output_path), quality=92)
    log.info(f"Colormap saved: {output_path.name}")
    return output_path


# ---------------------------------------------------------------------------
# Grayscale depth map for parallax viewer
# ---------------------------------------------------------------------------


def generate_depth_image(
    depth: np.ndarray,
    output_path: str | Path,
) -> Path:
    """
    Save a 16-bit grayscale PNG depth map for the WebGL parallax viewer.

    16-bit PNG avoids the banding artifacts you'd get from 8-bit JPEG,
    which matters for smooth parallax displacement.

    Bright = near (high displacement in shader), dark = far.
    """
    output_path = Path(output_path)

    depth_16bit = (depth * 65535).astype(np.uint16)
    Image.fromarray(depth_16bit, mode="I;16").save(str(output_path))

    log.info(f"Depth image saved: {output_path.name}")
    return output_path


# ---------------------------------------------------------------------------
# Colored point cloud PLY
# ---------------------------------------------------------------------------


def generate_pointcloud(
    rgb: np.ndarray,
    depth: np.ndarray,
    output_path: str | Path,
    downsample: int = 2,
    depth_scale: float = 1.0,
) -> Path:
    """
    Generate a colored point cloud PLY from RGB + depth.

    Uses synthetic camera intrinsics (since monocular depth is relative,
    not metric). The result is geometrically plausible but not metrically
    accurate — fine for visual exploration.

    Args:
        rgb: RGB uint8 array, shape (H, W, 3).
        depth: Float32 array (H, W), normalized [0, 1], 1 = nearest.
        output_path: Where to save the binary PLY.
        downsample: Take every Nth pixel (2 = half resolution).
            1 = full resolution (~31 MB for 1080p).
            2 = quarter pixels (~8 MB for 1080p).
            4 = 1/16th pixels (~2 MB for 1080p).
        depth_scale: Scale factor for Z values. Larger = more pronounced
            3D effect when viewing the point cloud.
    """
    output_path = Path(output_path)
    h, w = depth.shape

    # Convert depth convention: model gives 1=near, but for a point cloud
    # we want Z to increase with distance from camera.
    # Use inverse: Z = scale / (depth + epsilon) gives natural perspective.
    # But for simplicity and visual quality, we'll use:
    #   Z = (1 - depth) * depth_scale
    # so near objects (depth=1) have Z=0, far objects (depth=0) have Z=depth_scale.

    # Synthetic camera intrinsics (reasonable defaults for perspective projection)
    fx = fy = float(w)
    cx, cy = w / 2.0, h / 2.0

    # Build coordinate grids (downsampled)
    ys = np.arange(0, h, downsample)
    xs = np.arange(0, w, downsample)
    xx, yy = np.meshgrid(xs, ys)

    # Sample depth and RGB at grid points
    d = depth[ys[:, None], xs[None, :]]  # (h', w')
    colors = rgb[ys[:, None], xs[None, :]]  # (h', w', 3)

    # Skip pixels with negligible depth (very far / likely sky)
    mask = d > 0.02
    xx = xx[mask]
    yy = yy[mask]
    d = d[mask]
    colors = colors[mask]

    # Convert to 3D: Z increases away from camera
    z = (1.0 - d) * depth_scale
    x = (xx - cx) * z / fx
    y = (yy - cy) * z / fy

    n_points = len(x)
    log.info(
        f"Point cloud: {n_points:,} points "
        f"(downsample={downsample}x from {h}x{w})"
    )

    # Write binary PLY
    with open(output_path, "wb") as f:
        header = (
            "ply\n"
            "format binary_little_endian 1.0\n"
            f"element vertex {n_points}\n"
            "property float x\n"
            "property float y\n"
            "property float z\n"
            "property uchar red\n"
            "property uchar green\n"
            "property uchar blue\n"
            "end_header\n"
        )
        f.write(header.encode("ascii"))

        # Pack XYZ (float32) + RGB (uint8) for each point
        x32 = x.astype(np.float32)
        y32 = y.astype(np.float32)
        z32 = z.astype(np.float32)
        r = colors[:, 0].astype(np.uint8)
        g = colors[:, 1].astype(np.uint8)
        b = colors[:, 2].astype(np.uint8)

        for i in range(n_points):
            f.write(struct.pack("<fffBBB", x32[i], y32[i], z32[i], r[i], g[i], b[i]))

    file_size = output_path.stat().st_size
    log.info(f"Point cloud saved: {output_path.name} ({file_size / 1024 / 1024:.1f} MB)")
    return output_path
