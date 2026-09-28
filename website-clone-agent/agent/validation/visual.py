"""Visual accuracy scoring: original vs generated screenshots.

No vision model needed. For the full page and for every section we compute:
  * SSIM (structural similarity) on down-scaled grayscale images,
  * colour-histogram similarity (palette / backgrounds),
  * height ratio (layout/spacing drift).
The blended score is shown in the UI and drives the targeted "refine" pass,
which only re-prompts the worst sections (cheap, bounded).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageChops, ImageOps

WIDTH = 360


def _box_filter(img: np.ndarray, k: int) -> np.ndarray:
    pad = k // 2
    p = np.pad(img, pad, mode="edge")
    c = p.cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def ssim(a: np.ndarray, b: np.ndarray, k: int = 7) -> float:
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_a, mu_b = _box_filter(a, k), _box_filter(b, k)
    saa = _box_filter(a * a, k) - mu_a ** 2
    sbb = _box_filter(b * b, k) - mu_b ** 2
    sab = _box_filter(a * b, k) - mu_a * mu_b
    m = ((2 * mu_a * mu_b + c1) * (2 * sab + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (saa + sbb + c2))
    return float(np.clip(m.mean(), 0, 1))


def hist_sim(a: Image.Image, b: Image.Image) -> float:
    qa = np.asarray(a.convert("RGB").resize((64, 64)), dtype=np.int32) // 32
    qb = np.asarray(b.convert("RGB").resize((64, 64)), dtype=np.int32) // 32
    ia = (qa[..., 0] * 64 + qa[..., 1] * 8 + qa[..., 2]).ravel()
    ib = (qb[..., 0] * 64 + qb[..., 1] * 8 + qb[..., 2]).ravel()
    ha = np.bincount(ia, minlength=512) / ia.size
    hb = np.bincount(ib, minlength=512) / ib.size
    return float(np.minimum(ha, hb).sum())


def _scaled(im: Image.Image, width: int = WIDTH) -> Image.Image:
    return im.resize((width, max(1, int(im.height * width / im.width))))


def compare_images(a: Image.Image, b: Image.Image) -> dict:
    """Compare two images of possibly different heights (top-aligned)."""
    sa, sb = _scaled(a), _scaled(b)
    h = min(sa.height, sb.height)
    ga = np.asarray(ImageOps.grayscale(sa.crop((0, 0, WIDTH, h))), dtype=np.float64)
    gb = np.asarray(ImageOps.grayscale(sb.crop((0, 0, WIDTH, h))), dtype=np.float64)
    s = ssim(ga, gb) if h >= 8 else 0.0
    hs = hist_sim(sa, sb)
    ratio = min(a.height, b.height) / max(a.height, b.height, 1)
    score = 100 * (0.5 * s + 0.3 * hs + 0.2 * ratio)
    return {"score": round(score, 1), "ssim": round(s, 3), "color": round(hs, 3), "height_ratio": round(ratio, 3)}


def compare_pages(orig: Path, gen: Path) -> Optional[dict]:
    try:
        a, b = Image.open(orig).convert("RGB"), Image.open(gen).convert("RGB")
    except (OSError, FileNotFoundError):
        return None
    out = compare_images(a, b)
    out["orig_height"], out["gen_height"] = a.height, b.height
    return out


def section_scores(orig: Path, gen: Path, orig_boxes: dict, gen_boxes: list) -> dict:
    """Per-section comparison. orig_boxes: name -> [x, y, w, h]; gen_boxes: [{name,x,y,w,h}]."""
    try:
        a, b = Image.open(orig).convert("RGB"), Image.open(gen).convert("RGB")
    except (OSError, FileNotFoundError):
        return {}
    out = {}
    gmap = {g["name"]: g for g in gen_boxes}
    for name, box in orig_boxes.items():
        g = gmap.get(name)
        if not g or not box or box[3] < 4 or g["h"] < 4:
            continue
        if box[1] >= a.height - 4 or g["y"] >= b.height - 4:
            continue  # beyond the (height-capped) screenshot
        ca = a.crop((0, box[1], a.width, min(a.height, box[1] + box[3])))
        cb = b.crop((0, g["y"], b.width, min(b.height, g["y"] + g["h"])))
        if ca.height < 4 or cb.height < 4:
            continue
        r = compare_images(ca, cb)
        r.update({"orig_h": box[3], "gen_h": g["h"]})
        out[name] = r
    return out


def side_by_side(orig: Path, gen: Path, out: Path, width: int = 480, max_h: int = 4000) -> Optional[Path]:
    """Original | Generated | Difference strip used by the UI."""
    try:
        a, b = _scaled(Image.open(orig).convert("RGB"), width), _scaled(Image.open(gen).convert("RGB"), width)
    except (OSError, FileNotFoundError):
        return None
    h = min(max(a.height, b.height), max_h)
    canvas = Image.new("RGB", (width * 3 + 20, h), (245, 245, 245))
    canvas.paste(a.crop((0, 0, width, min(h, a.height))), (0, 0))
    canvas.paste(b.crop((0, 0, width, min(h, b.height))), (width + 10, 0))
    common = min(a.height, b.height, h)
    diff = ImageChops.difference(a.crop((0, 0, width, common)), b.crop((0, 0, width, common)))
    diff = ImageOps.autocontrast(ImageOps.grayscale(diff)).convert("RGB")
    canvas.paste(diff, (2 * width + 20, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out
