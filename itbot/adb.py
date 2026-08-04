"""Minimal ADB wrapper - screenshot / tap / swipe via the adb binary.

No uiautomator, no extra deps. Works with MuMu Player (default port 16384)
or any emulator adb can see.
"""
import re
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

    def list_displays(self):
        """All display ids the emulator exposes (MuMu Nx runs apps on their
        own virtual displays, so the game may not be on the first one)."""
        try:
            out = self._run(["shell", "dumpsys", "SurfaceFlinger", "--display-id"],
                            timeout=15)
            return re.findall(r"Display\s+(\d+)", out or "")
        except Exception:
            return []

    def set_display(self, did):
        self._disp = did
        self._trim_logged = False

    def _display_id(self):
        """MuMu Nx exposes several displays and warns that screencap's
        default pick 'is not guaranteed to be consistent across captures',
        so pin one explicitly. Cached after the first lookup."""
        if hasattr(self, "_disp"):
            return self._disp
        self._disp = None
        try:
            out = self._run(["shell", "dumpsys", "SurfaceFlinger", "--display-id"],
                            timeout=15)
            ids = re.findall(r"Display\s+(\d+)", out or "")
            if ids:
                self._disp = ids[0]
                self.log(f"[ADB] multiple displays found - pinning display {self._disp}")
        except Exception:
            pass
        return self._disp

    def screenshot(self):
        """Returns BGR image or None.

        exec-out is the fast path, but some emulator builds (MuMu Nx) return
        nothing for it - fall back to screencap-to-file + pull, and finally
        to `shell screencap -p` with CRLF repair."""
        try:
            did = self._display_id()
            cmd = ["exec-out", "screencap", "-p"] + (["-d", did] if did else [])
            raw = self._run(cmd, timeout=20, binary=True)
            if (not raw or len(raw) < 1000) and did:
                raw = self._run(["exec-out", "screencap", "-p"], timeout=20, binary=True)
            if not raw or len(raw) < 1000:
                raw = self._screencap_fallback()
            if not raw:
                if not getattr(self, "_cap_diag", False):
                    self._cap_diag = True
                    self.log("[ADB] every screenshot method returned no data - "
                             "the emulator may block screencap")
                return None
            # MuMu Nx's adb prints a "[Warning] ..." line before the image
            # data, so the payload does not start at byte 0. Cut to the PNG
            # magic header (or the JPEG one, just in case).
            for magic in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
                i = raw.find(magic)
                if i > 0:
                    if not getattr(self, "_trim_logged", False):
                        self._trim_logged = True
                        self.log(f"[ADB] trimming {i} bytes of adb warning text "
                                 f"before each screenshot")
                    raw = raw[i:]
                    break
                if i == 0:
                    break
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                if not getattr(self, "_cap_diag", False):
                    self._cap_diag = True
                    self.log(f"[ADB] got {len(raw)} bytes but could not decode them "
                             f"(first bytes: {raw[:8]!r}) - wrong format, not a PNG")
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

    def _screencap_fallback(self):
        """Older routes to a screenshot, for devices where exec-out is empty
        or unsupported (MuMu Nx). Tries: screencap to /data/local/tmp + pull,
        screencap to /sdcard + cat, then plain shell screencap."""
        import os
        import tempfile
        for remote in ("/data/local/tmp/_bb.png", "/sdcard/_bb.png"):
            try:
                self._run(["shell", "screencap", "-p", remote], timeout=25)
                local = os.path.join(tempfile.gettempdir(), "_batibot_cap.png")
                if os.path.exists(local):
                    os.remove(local)
                self._run(["pull", remote, local], timeout=25)
                if os.path.exists(local) and os.path.getsize(local) > 1000:
                    with open(local, "rb") as f:
                        raw = f.read()
                    if not getattr(self, "_fallback_logged", False):
                        self._fallback_logged = True
                        self.log(f"[ADB] screenshots via screencap+pull ({remote})")
                    return raw
            except Exception:
                pass
        try:
            self._run(["shell", "screencap", "-p", "/sdcard/_bb.png"], timeout=25)
            raw = self._run(["exec-out", "cat", "/sdcard/_bb.png"], timeout=25, binary=True)
            if raw:
                if not getattr(self, "_fallback_logged", False):
                    self._fallback_logged = True
                    self.log("[ADB] using the screencap+cat fallback for screenshots")
                return raw
        except Exception:
            pass
        try:
            raw = self._run(["shell", "screencap", "-p"], timeout=25, binary=True)
            if raw:
                raw = raw.replace(b"\r\n", b"\n")     # shell mangles newlines
                if not getattr(self, "_fallback_logged", False):
                    self._fallback_logged = True
                    self.log("[ADB] using the shell screencap fallback for screenshots")
                return raw
        except Exception:
            pass
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
                # how long the bot spent between this tap and the last one,
                # so a slow session can be read straight off the log
                now = time.time()
                gap = now - getattr(self, "_last_tap_t", now)
                self._last_tap_t = now
                self.log(f"[TAP] ({int(x)},{int(y)}) {desc} +{gap:.1f}s")
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
