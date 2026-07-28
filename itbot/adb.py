"""Minimal ADB wrapper - screenshot / tap / swipe via the adb binary.

No uiautomator, no extra deps. Works with MuMu Player (default port 16384)
or any emulator adb can see.
"""
import subprocess
import time

import cv2
import numpy as np


class Adb:
    def __init__(self, adb_path="adb", address="127.0.0.1:16384", log=print):
        self.adb_path = adb_path
        self.address = address
        self.log = log
        # every coordinate in this bot is 720x1280. If the emulator runs at
        # another resolution we scale screenshots down to 720x1280 and scale
        # taps/swipes back up, so the bot works anywhere. `native` keeps the
        # full-resolution frame for sharper OCR when running above 720p.
        self.sx = 1.0
        self.sy = 1.0
        self.native = None
        self._res_logged = False

    def _run(self, args, timeout=15, binary=False):
        cmd = [self.adb_path, "-s", self.address] + args
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.stdout if binary else r.stdout.decode(errors="ignore")

    def connect(self) -> bool:
        try:
            out = subprocess.run([self.adb_path, "connect", self.address],
                                 capture_output=True, timeout=10).stdout.decode(errors="ignore")
            ok = "connected" in out or "already" in out
            if not ok:
                self.log(f"[ADB] connect failed: {out.strip()}")
            return ok
        except Exception as e:
            self.log(f"[ADB] connect error: {e} (is adb on PATH / adb_path correct?)")
            return False

    def screenshot(self):
        """Returns BGR image or None."""
        try:
            raw = self._run(["exec-out", "screencap", "-p"], timeout=20, binary=True)
            if not raw:
                return None
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return None
            h, w = img.shape[:2]
            if (w, h) == (720, 1280):
                self.sx = self.sy = 1.0
                self.native = None
                return img
            self.sx, self.sy = w / 720.0, h / 1280.0
            self.native = img
            if not self._res_logged:
                self._res_logged = True
                self.log(f"[ADB] emulator is {w}x{h} - auto-scaling to 720x1280 "
                         f"(720x1280 portrait is still the recommended setting)")
                if abs((w / h) - 0.5625) > 0.02:
                    self.log("[ADB] WARNING: that is not a 9:16 portrait screen - "
                             "taps may be off. Set MuMu to 720x1280 (DPI 240).")
            return cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        except Exception as e:
            self.log(f"[ADB] screenshot error: {e}")
            return None

    def tap(self, x, y, desc=""):
        try:
            self._run(["shell", "input", "tap",
                       str(int(x * self.sx)), str(int(y * self.sy))])
            key = (int(x), int(y), desc)
            if key == getattr(self, "_last_tap", None):
                self.repeat_taps = getattr(self, "repeat_taps", 0) + 1
            else:
                self.repeat_taps = 0
                self._last_tap = key
            if desc:
                self.log(f"[TAP] ({int(x)},{int(y)}) {desc}")
            time.sleep(0.25)
        except Exception as e:
            self.log(f"[ADB] tap error: {e}")

    def swipe(self, x1, y1, x2, y2, dur_ms=400):
        try:
            self._run(["shell", "input", "swipe",
                       str(int(x1 * self.sx)), str(int(y1 * self.sy)),
                       str(int(x2 * self.sx)), str(int(y2 * self.sy)),
                       str(int(dur_ms))])
            time.sleep(0.35)
        except Exception as e:
            self.log(f"[ADB] swipe error: {e}")
