"""Download and normalize images to Instagram's publishing constraints.

Two reasons this module exists rather than hotlinking `media_url` directly:

1. Meta's `media_url` values are signed CDN URLs that EXPIRE (hours to days).
   The publish step runs on a later cron tick than discovery, so by the time we
   POST /media the original URL is often dead. We must re-host.

2. Content publishing has hard input requirements that source posts routinely
   violate. From the docs: "JPEG is the only image format supported." Plus
   width must land in 320-1440px and the aspect ratio must sit between 4:5 and
   1.91:1, with an 8MB ceiling. A PNG, a 2160px-wide shot, or a tall 9:16 story
   crop all fail container creation. Normalizing up front turns those failures
   into successful posts.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import requests
from PIL import Image, ImageFilter

log = logging.getLogger(__name__)

# Instagram content-publishing image constraints.
MIN_WIDTH = 320
MAX_WIDTH = 1440
MIN_ASPECT = 4 / 5      # 0.80 -- tallest allowed (portrait)
MAX_ASPECT = 1.91       # widest allowed (landscape)
MAX_BYTES = 8 * 1024 * 1024

DOWNLOAD_TIMEOUT = 60
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024  # refuse absurd payloads


class ImageError(RuntimeError):
    pass


def download(url: str) -> bytes:
    """Fetch bytes from a (possibly short-lived) CDN URL."""
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise ImageError(f"download failed: {exc}") from exc

    if not resp.ok:
        raise ImageError(f"download returned HTTP {resp.status_code}")

    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(64 * 1024):
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            raise ImageError(f"image exceeds {MAX_DOWNLOAD_BYTES} byte ceiling")
        chunks.append(chunk)

    data = b"".join(chunks)
    if not data:
        raise ImageError("download produced zero bytes")
    return data


def _pad_to_aspect(img: Image.Image, target_aspect: float) -> Image.Image:
    """Letterbox onto a blurred copy of itself to reach a legal aspect ratio.

    Blurred bars rather than solid black: it keeps the post looking deliberate
    instead of broken, which matters for a feed nobody is reviewing by hand.
    """
    w, h = img.size
    if target_aspect >= w / h:
        new_w, new_h = int(round(h * target_aspect)), h
    else:
        new_w, new_h = w, int(round(w / target_aspect))

    new_w = max(new_w, w)
    new_h = max(new_h, h)

    background = img.resize((new_w, new_h), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(radius=max(new_w, new_h) // 40 or 1)
    )
    background.paste(img, ((new_w - w) // 2, (new_h - h) // 2))
    return background


def normalize(data: bytes, dest: Path) -> dict:
    """Write `data` to `dest` as an Instagram-publishable JPEG.

    Returns metadata about what was produced (and what had to be changed).
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # Pillow raises a wide variety here
        raise ImageError(f"not a decodable image: {exc}") from exc

    notes: list[str] = []
    original_format = img.format
    original_size = img.size

    # Flatten alpha / palette / CMYK onto white; JPEG cannot carry alpha.
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        img = flat
        notes.append("flattened transparency")
    elif img.mode != "RGB":
        img = img.convert("RGB")
        notes.append(f"converted {img.mode} to RGB")

    # Aspect ratio into the legal band.
    w, h = img.size
    aspect = w / h
    if aspect < MIN_ASPECT:
        img = _pad_to_aspect(img, MIN_ASPECT)
        notes.append(f"padded aspect {aspect:.3f} up to {MIN_ASPECT:.3f}")
    elif aspect > MAX_ASPECT:
        img = _pad_to_aspect(img, MAX_ASPECT)
        notes.append(f"padded aspect {aspect:.3f} down to {MAX_ASPECT:.3f}")

    # Width into the legal band, preserving the (now legal) aspect ratio.
    w, h = img.size
    if w > MAX_WIDTH:
        new_h = max(1, int(round(h * MAX_WIDTH / w)))
        img = img.resize((MAX_WIDTH, new_h), Image.LANCZOS)
        notes.append(f"downscaled width {w} to {MAX_WIDTH}")
    elif w < MIN_WIDTH:
        new_h = max(1, int(round(h * MIN_WIDTH / w)))
        img = img.resize((MIN_WIDTH, new_h), Image.LANCZOS)
        notes.append(f"upscaled width {w} to {MIN_WIDTH}")

    # Encode, stepping quality down until it fits the 8MB ceiling.
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = b""
    for quality in (92, 87, 82, 75, 68, 60):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        payload = buf.getvalue()
        if len(payload) <= MAX_BYTES:
            break
    else:
        raise ImageError(f"cannot fit image under {MAX_BYTES} bytes")

    dest.write_bytes(payload)

    final_w, final_h = img.size
    return {
        "path": dest.name,
        "bytes": len(payload),
        "width": final_w,
        "height": final_h,
        "aspect": round(final_w / final_h, 4),
        "original_format": original_format,
        "original_size": list(original_size),
        "adjustments": notes,
    }


def fetch_and_normalize(url: str, dest: Path) -> dict:
    return normalize(download(url), dest)
