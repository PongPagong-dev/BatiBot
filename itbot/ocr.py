"""PaddleOCR wrapper - full-screen text boxes.

Returns a list of (text, cx, cy, x1, y1, x2, y2). Lazy-initialized because
PaddleOCR takes a few seconds to load.
"""
import threading

_ocr = None
_lock = threading.Lock()


def _get_ocr():
    global _ocr
    with _lock:
        if _ocr is None:
            from paddleocr import PaddleOCR
            _ocr = PaddleOCR(use_angle_cls=False, lang="en", show_log=False)
        return _ocr


# v1.11: the OCR speed baseline must survive UI Stop/Start (which makes a
# new Bot object in the SAME process) - kept here at module level so a
# restarted Bot cannot re-baseline at an already-drifted speed.
PROCESS_BASELINE = [None]


def reset_ocr():
    """Throw the reader away so the next read builds a fresh one.

    PaddleOCR gets slower the longer it lives: on the 06/08 run the median
    read grew from 1.2s to 2.0s over ten hours while screenshot time stayed
    flat at 0.3s, and restarting the bot reset it. Rebuilding costs ~10s of
    model loading, which a few careers of drift pays for many times over."""
    global _ocr
    with _lock:
        _ocr = None
    import gc
    gc.collect()


_err_count = 0


def ocr_boxes(img_bgr):
    """OCR the whole image, return [(text, cx, cy, x1, y1, x2, y2), ...]."""
    global _err_count
    if img_bgr is None:
        return []
    try:
        result = _get_ocr().ocr(img_bgr, cls=False)
    except Exception:
        # do not fail silently - print the real error (first 3 times)
        if _err_count < 3:
            _err_count += 1
            import traceback
            print("[OCR ERROR] PaddleOCR failed:", flush=True)
            traceback.print_exc()
        return []
    boxes = []
    if not result:
        return boxes
    lines = result[0] if isinstance(result[0], list) else result
    if not lines:
        return boxes
    for line in lines:
        try:
            pts, (text, conf) = line
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
            boxes.append((text.strip(), (x1 + x2) / 2, (y1 + y2) / 2, x1, y1, x2, y2))
        except Exception:
            continue
    return boxes


def ocr_text(img_bgr):
    """All OCR text concatenated (uppercased) - for 'is X on screen' checks."""
    return " | ".join(b[0] for b in ocr_boxes(img_bgr)).upper()
