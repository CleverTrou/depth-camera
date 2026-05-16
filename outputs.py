"""
Output generators — turn RGB + depth map into visual formats.

All functions accept a normalized depth map where 1.0 = nearest, 0.0 = farthest.
"""

import logging
import struct
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Point cloud geometry helpers
# ---------------------------------------------------------------------------


def _ransac_ground_plane(
    x: np.ndarray, y: np.ndarray, z: np.ndarray,
    ground_mask: np.ndarray,
    n_iter: int = 60,
    threshold_frac: float = 0.04,
) -> np.ndarray | None:
    """Fit a plane to ground-candidate points via RANSAC.

    Returns a unit normal pointing toward the camera (−Y direction in
    camera space), or None if no clean plane is found.
    """
    gx, gy, gz = x[ground_mask], y[ground_mask], z[ground_mask]
    n = len(gx)
    if n < 10:
        return None

    pts = np.stack([gx, gy, gz], axis=1)
    z_range = float(gz.max() - gz.min())
    thresh = max(threshold_frac * z_range, 1e-4)
    min_inliers = max(50, int(n * 0.25))

    rng = np.random.default_rng(42)
    best_count, best_normal = 0, None

    for _ in range(n_iter):
        idx = rng.choice(n, 3, replace=False)
        p = pts[idx]
        v1, v2 = p[1] - p[0], p[2] - p[0]
        normal = np.cross(v1, v2)
        nl = np.linalg.norm(normal)
        if nl < 1e-8:
            continue
        normal /= nl
        d = float(np.dot(normal, p[0]))
        count = int((np.abs(pts @ normal - d) < thresh).sum())
        if count > best_count:
            best_count, best_normal = count, normal.copy()

    if best_normal is None or best_count < min_inliers:
        return None

    # Ensure normal points up toward camera (negative Y in camera coords)
    if best_normal[1] > 0:
        best_normal = -best_normal
    return best_normal


def _rotation_align(src: np.ndarray, dst: np.ndarray = np.array([0., -1., 0.])) -> np.ndarray:
    """3×3 rotation matrix mapping unit vector src onto dst (Rodrigues formula)."""
    v = np.cross(src, dst)
    s = np.linalg.norm(v)
    c = float(np.dot(src, dst))
    if s < 1e-8:
        return np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1.0 - c) / s ** 2)

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
    hfov_deg: float = 113.0,
    ground_correction: bool = True,
) -> Path:
    """
    Generate a colored point cloud PLY from RGB + depth.

    Args:
        rgb: RGB uint8 array, shape (H, W, 3).
        depth: Float32 array (H, W), normalized [0, 1], 1 = nearest.
        output_path: Where to save the binary PLY.
        downsample: Take every Nth pixel (2 = half resolution).
        depth_scale: Scale factor for Z values.
        hfov_deg: Camera horizontal field of view in degrees.
            Aqara G5 Pro: ~113° (derived from 120° diagonal spec).
            Using the actual FOV instead of the former fx=w shorthand
            corrects ground-plane bowing caused by wrong projection geometry.
        ground_correction: If True, fit a plane to the lower image region
            via RANSAC and rotate the point cloud so the ground is flat.
    """
    output_path = Path(output_path)
    h, w = depth.shape

    # Correct camera intrinsics from horizontal FOV
    fx = fy = (w / 2.0) / np.tan(np.radians(hfov_deg / 2.0))
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

    # Convert to 3D with perspective depth correction.
    # The depth model predicts along-ray distance; we need orthogonal z-depth.
    # For a pixel at angle θ from the optical axis: z_ortho = z_ray * cos(θ).
    # Without this, wide-angle edge pixels appear ~45% too deep, creating the
    # characteristic bowl/U distortion on flat surfaces like a patio floor.
    z_ray = 1.0 - d
    nx = (xx - cx) / fx  # normalised ray direction (= tan θ_x)
    ny = (yy - cy) / fy  # normalised ray direction (= tan θ_y)
    cos_theta = 1.0 / np.sqrt(1.0 + nx ** 2 + ny ** 2)
    z_ortho = z_ray * cos_theta  # orthogonal depth

    x = nx * z_ortho
    y = ny * z_ortho
    z = z_ortho * depth_scale

    # RANSAC ground-plane correction: fit a plane to the lower 40% of the
    # frame, then rotate the whole cloud so that plane is horizontal.
    if ground_correction:
        ground_mask = yy >= int(0.6 * h)
        normal = _ransac_ground_plane(x, y, z, ground_mask)
        if normal is not None:
            R = _rotation_align(normal)
            pts = np.stack([x, y, z], axis=1) @ R.T
            x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
            log.info(
                f"Ground plane corrected via RANSAC "
                f"(normal=[{normal[0]:.3f}, {normal[1]:.3f}, {normal[2]:.3f}])"
            )
        else:
            log.info("RANSAC ground correction skipped (no dominant plane found)")

    n_points = len(x)
    log.info(
        f"Point cloud: {n_points:,} points "
        f"(downsample={downsample}x from {h}x{w}, hfov={hfov_deg:.0f}°)"
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
