"""
Monocular depth estimation using Depth Anything V2 (Small) via ONNX Runtime.

Runs entirely on CPU — no GPU required. On a Pi 5 (Cortex-A76 quad-core),
expect ~3-6 seconds per frame at 518x518 model input resolution.

The model is downloaded on first use from HuggingFace and cached locally.

Output convention:
    The depth map is normalized to [0.0, 1.0] where:
        1.0 = nearest to camera
        0.0 = farthest from camera
    This convention is used consistently across all output formats.
"""

import logging
import os
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger("depth")

# ImageNet normalization (used by Depth Anything V2's DINOv2 backbone)
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Default model URL (Depth Anything V2 Small, ONNX)
# This points to the ONNX Community's pre-exported model on HuggingFace.
# If this URL stops working, see README for manual export instructions.
DEFAULT_MODEL_URL = (
    "https://huggingface.co/onnx-community/depth-anything-v2-small"
    "/resolve/main/onnx/model.onnx"
)

_session = None


def _download_model(url: str, dest: Path):
    """Download the ONNX model file with a progress indicator."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Downloading depth model to {dest}...")
    log.info(f"  URL: {url}")
    log.info("  This is a one-time download (~50 MB). Please wait...")

    try:
        tmp = dest.with_suffix(".tmp")
        urllib.request.urlretrieve(url, str(tmp))
        tmp.rename(dest)
        size_mb = dest.stat().st_size / 1024 / 1024
        log.info(f"  Downloaded {size_mb:.1f} MB")
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(
            f"Failed to download depth model: {e}\n"
            f"You can download it manually:\n"
            f"  curl -L '{url}' -o '{dest}'\n"
            f"Or export from PyTorch (see README)."
        ) from e


def load_model(
    model_path: str | None = None,
    model_url: str | None = None,
):
    """
    Load the ONNX model, downloading if necessary.

    Args:
        model_path: Path to the .onnx file. If None, uses default cache location.
        model_url: URL to download from. If None, uses the default HuggingFace URL.

    Returns:
        An ONNX Runtime InferenceSession.
    """
    global _session
    if _session is not None:
        return _session

    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError(
            "onnxruntime is required: pip install onnxruntime"
        )

    if model_path is None:
        model_path = os.path.expanduser(
            "~/.cache/depth-camera/depth_anything_v2_vits.onnx"
        )
    model_path = Path(model_path)

    if not model_path.exists():
        url = model_url or DEFAULT_MODEL_URL
        _download_model(url, model_path)

    log.info(f"Loading depth model: {model_path.name}")
    _session = ort.InferenceSession(
        str(model_path),
        providers=["CPUExecutionProvider"],
    )

    input_info = _session.get_inputs()[0]
    log.info(
        f"  Input: {input_info.name} {input_info.shape} ({input_info.type})"
    )
    return _session


def estimate_depth(
    image: np.ndarray,
    session=None,
    model_input_size: int = 518,
) -> np.ndarray:
    """
    Estimate depth from an RGB image.

    Args:
        image: RGB uint8 numpy array, shape (H, W, 3).
        session: ONNX Runtime session. If None, loads/caches the default model.
        model_input_size: Resize input to this square size for the model.
            518 is the Depth Anything V2 default. Use 384 for ~40% faster
            inference at slightly lower quality.

    Returns:
        Depth map as float32 numpy array, shape (H, W), values in [0, 1].
        Convention: 1.0 = nearest, 0.0 = farthest.
    """
    if session is None:
        session = load_model()

    orig_h, orig_w = image.shape[:2]

    # Preprocess: resize, normalize, transpose to NCHW
    from PIL import Image

    pil_img = Image.fromarray(image)
    pil_resized = pil_img.resize(
        (model_input_size, model_input_size), Image.BILINEAR
    )
    arr = np.array(pil_resized, dtype=np.float32) / 255.0
    arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
    arr = arr.transpose(2, 0, 1)  # HWC → CHW
    batch = arr[np.newaxis, ...]  # add batch dim → NCHW

    # Run inference
    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: batch})
    raw_depth = outputs[0]

    # Handle various output shapes: (1,1,H,W), (1,H,W), (H,W)
    raw_depth = np.squeeze(raw_depth)  # → (H, W)

    # Resize back to original resolution
    depth_pil = Image.fromarray(raw_depth)
    depth_resized = depth_pil.resize((orig_w, orig_h), Image.BILINEAR)
    depth = np.array(depth_resized, dtype=np.float32)

    # Normalize to [0, 1].
    # Depth Anything outputs inverse depth (higher = nearer).
    # Normalize so max (nearest) = 1.0, min (farthest) = 0.0.
    d_min, d_max = depth.min(), depth.max()
    if d_max - d_min > 1e-6:
        depth = (depth - d_min) / (d_max - d_min)
    else:
        depth = np.zeros_like(depth)

    return depth
