"""PaddleOCR wrapper (Baidu PP-OCRv6) with LLM-vision fallback.

English rationale: PP-OCR gives deterministic, high-confidence transcription
for receipts/screenshots; LLM OCR stays as fallback when Paddle is unavailable.
"""

import logging
import threading
from pathlib import Path

from .llm import OmlxClient, safe_ocr

_engine = None
_engine_lock = threading.Lock()


def get_engine():
    """Lazy singleton; predict() serialized by the wrapper's lock."""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            from paddleocr import PaddleOCR
            _engine = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                lang="ch",
            )
    return _engine


def _merge_to_lines(boxes) -> str:
    """Sort boxes into visual lines (y bucket) then left-to-right within a line."""
    if not boxes:
        return ""
    heights = sorted(b[2] for b in boxes)
    median_h = heights[len(heights) // 2] or 12
    boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    lines: list[list] = []
    for b in boxes:
        placed = False
        for line in lines:
            ref_y = line[0][1]
            if abs(b[1] - ref_y) <= max(median_h * 0.7, 10):
                line.append(b)
                placed = True
                break
        if not placed:
            lines.append([b])
    out_lines = []
    for line in lines:
        line.sort(key=lambda b: b[0])
        out_lines.append(" ".join(b[4] for b in line))
    return "\n".join(out_lines).strip() or ""


def ocr_image_text(path) -> str | None:
    """Run PaddleOCR and return text in visual order; None on failure/empty."""
    try:
        engine = get_engine()
        result = engine.predict(str(path))
        if not result:
            return None
        res = result[0].json.get("res", {})
        texts = res.get("rec_texts") or []
        polys = res.get("rec_polys") or []
        boxes = []
        for text, poly in zip(texts, polys):
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            boxes.append((min(xs), min(ys), max(ys) - min(ys), max(xs), text))
        text_out = _merge_to_lines(boxes)
        return text_out or None
    except Exception:  # noqa: BLE001 — OCR 失败交由调用方兜底
        logging.getLogger(__name__).exception("paddle ocr failed")
        return None


def downscale_for_llm(path, max_side: int = 1280):
    """大图缩到 max_side 内再喂 LLM（省视觉 token）；小图原样返回。"""
    from PIL import Image
    path = Path(path)
    try:
        with Image.open(path) as im:
            w, h = im.size
            if max(w, h) <= max_side:
                return path
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            out = path.with_name(f"{path.stem}_llm.jpg")
            im.save(out, quality=85)
            return out
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("downscale failed")
        return path


def warmup() -> None:
    """服务启动时预热 Paddle（消除首图冷启动 3s+）。"""
    try:
        get_engine()
        from PIL import Image
        probe = Image.new("RGB", (64, 32), "white")
        tmp = Path("/tmp/glimpsely_ocr_probe.png")
        probe.save(tmp)
        ocr_image_text(tmp)
        logging.getLogger(__name__).info("paddle ocr warmed up")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("paddle warmup failed")


def full_ocr(client: OmlxClient | None, image_path) -> str | None:
    """Paddle first (deterministic), LLM vision as fallback."""
    text = ocr_image_text(image_path)
    if text:
        return text
    if client is not None:
        return safe_ocr(client, image_path)
    return None
