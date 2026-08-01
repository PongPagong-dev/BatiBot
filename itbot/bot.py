"""UMA IT BOT - Independent Training loop, OCR-driven state machine.

Philosophy: no image templates. Every screen is recognized by the text on
it, and buttons are clicked where OCR finds their labels. A hard blocklist
makes destructive buttons unclickable no matter what.

Loop per career:
  home -> CAREER -> career setup (reuses the game's last-used trainee/
  legacy/deck via Next-clicks; borrow card picked by name from settings)
  -> Final Confirmation -> Independent Training tab -> Start!
  -> sleep ~50 min (reads "Time Left h:mm:ss" from the status popup)
  -> TRAINING COMPLETE -> Career -> skill buying (single down sweep,
  names from settings) -> Complete Career -> sparks (optional one reroll,
  keeps the starrier set) -> results -> To Home -> next career.
"""
import json
import os
import re
import time
import threading

import cv2
import numpy as np
from rapidfuzz import fuzz

from .adb import Adb
from .ocr import ocr_boxes


def _norm_name(n):
    return re.sub(r'[^A-Z0-9]', '', n.upper())


def _load_cards():
    """normalized card title -> uma name (from cards.json)."""
    try:
        with open("cards.json", encoding="utf-8") as f:
            out = {}
            for c in json.load(f):
                chara = (c.get("desc", "") or "").split("(")[0].strip()
                out[_norm_name(c.get("name", ""))] = chara
            return out
    except Exception:
        return {}


def _load_skill_values():
    """skill_values.json: normalized name -> {n: display, g: rating grade,
    c: base cost, r: rarity}. Extracted from the game's master data."""
    try:
        with open("skill_values.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# ---- fixed coordinates (720x1280) --------------------------------------
IT_TAB = (531, 216)          # "Independent Training" tab on Final Confirmation
IT_START = (517, 1182)       # green Start! (same spot as Start Career!)
POPUP_CANCEL = (201, 833)    # IT status popup
POPUP_CAREER = (517, 833)
HOME_CAREER = (545, 1075)    # CAREER button on home
GUEST_SLOT = (570, 680)      # friend/guest slot on Support Formation
NEUTRAL_TAP = (360, 1150)    # advance splash screens
CAROUSEL_LEFT = (57, 150)    # spark set carousel arrows
CAROUSEL_RIGHT = (663, 150)

TIMER_RE = re.compile(r"(\d+)\D(\d{1,2})\D(\d{1,2})")

# Printed at startup so the log always says which code is actually running.
# (01/08: a fix looked broken for 5 hours because the bot had never been
# restarted after the deploy - the log gave no way to tell.)
VERSION = "0.73"

# how long the picture may stay completely unchanged before we treat the
# game as hung, and how long we wait after relaunching it
FREEZE_SECONDS = 240
FREEZE_RELAUNCH_WAIT = 90

# Longest single nap while training runs. The bot takes no screenshots while
# it sleeps, so a frozen game can only be spotted when it wakes: napping in
# quarter-hour pieces means a freeze is caught within ~15 minutes instead of
# at the end of the whole 45-50 minute session.
SLEEP_CHUNK = 900

# buttons the generic clicker may press
ALLOW_BUTTONS = ["NEXT", "CLOSE", "CONFIRM", "OK", "TO HOME", "START CAREER!", "START CAREER"]
# buttons that must NEVER be pressed, anywhere, ever
# ("Save Here" would overwrite the user's saved agenda with the current one)
BLOCKLIST = ["DELETE DATA", "GIVE UP", "RECOVER", "EDIT TEAM", "RETIRE", "SAVE HERE"]


def _find(boxes, label, min_ratio=85, y_min=0, y_max=1280):
    """Find the OCR box best matching label. Returns (cx, cy) or None."""
    label_u = label.upper()
    best, best_r = None, 0
    for text, cx, cy, *_ in boxes:
        if not (y_min <= cy <= y_max):
            continue
        t = text.upper()
        if any(b in t for b in BLOCKLIST):
            continue
        r = fuzz.ratio(t, label_u)
        if label_u in t:
            r = max(r, 96)
        if r >= min_ratio and r > best_r:
            best, best_r = (cx, cy), r
    return best


def _has(boxes, label, min_ratio=85):
    return _find(boxes, label, min_ratio) is not None


def x_left(bx):
    """Name column on the Learn screen (left of the cost cluster)."""
    return bx < 500


def _all_text(boxes):
    return " | ".join(b[0] for b in boxes).upper()


class ItBot:
    def __init__(self, settings, log):
        self.s = settings
        self.log = log
        self.adb = Adb(settings.get("adb_path", "adb"),
                       settings.get("adb_address", "127.0.0.1:16384"), log)
        self._stop = threading.Event()
        self._thread = None
        self.values = _load_skill_values()
        self.cards = _load_cards()
        # UI status
        self.state = "idle"
        self.careers_done = 0
        self.sleep_until = 0
        # per-career flags
        self._borrow_done = False
        self._skills_done = False
        self._rerolled = False
        self._session_started = False
        self._agenda_done = False
        self._agenda_checked = False
        self._focus_done = False
        self._same_state_count = 0
        self._last_state = ""
        self._spark_a = None
        self._spark_b = None
        self._carousel_flips = 0
        self._carousel_logged = False

    # ---- lifecycle ------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.state = "stopping"

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _set_state(self, st):
        if st == self._last_state:
            self._same_state_count += 1
        else:
            self._same_state_count = 0
            self._last_state = st
        self.state = st

    def _sleep(self, seconds):
        """Interruptible sleep."""
        self.sleep_until = time.time() + seconds
        end = self.sleep_until
        while time.time() < end and not self._stop.is_set():
            time.sleep(min(15, max(0.5, end - time.time())))
        self.sleep_until = 0
        return not self._stop.is_set()

    def _capture_training_log(self, boxes, txt):
        """Pull the final numbers off whichever end-of-career screen we are
        on (Training Log Overview, or the results/details screen - they use
        different layouts). Values sit either BELOW their stat label or
        beside it, so both are accepted; nearest match wins."""
        try:
            nums = []
            for t, cx, cy, *_ in boxes:
                d = t.replace(",", "").strip()
                if not (d.isdigit() and 2 <= len(d) <= 4):
                    # the value often arrives glued to its rank letter
                    # ("UG1265", "A836", "C+599") - take the trailing digits
                    m2 = re.search(r"([0-9]{2,4})$", d)
                    d = m2.group(1) if m2 else ""
                if d.isdigit() and 2 <= len(d) <= 4 and 1 <= int(d) <= 2500:
                    nums.append((int(d), cx, cy))
            stats = dict(getattr(self, "_stats", {}) or {})
            labels = []
            for label, key in (("Speed", "spd"), ("Stamina", "sta"), ("Power", "pow"),
                               ("Guts", "gut"), ("Wit", "wit")):
                lp = _find(boxes, label, 88)
                if lp:
                    labels.append((lp[0], lp[1], key))
            if len(labels) >= 3:
                # the five stats sit in one horizontal row. Align labels and
                # numbers BY ORDER (both sorted left to right) with a two
                # pointer walk - distance matching kept mis-assigning when a
                # value OCR'd slightly off centre (30/07: only 'wit' landed).
                row_y = sorted(l[1] for l in labels)[len(labels) // 2]
                on_row = sorted((cx, v) for v, cx, cy in nums
                                if -45 <= (cy - row_y) <= 140)
                labels.sort()
                if len(on_row) == len(labels):
                    for (_lx, _ly, key), (_cx, v) in zip(labels, on_row):
                        stats[key] = int(v)
                else:
                    i = 0
                    for lx, _ly, key in labels:
                        while i < len(on_row) and on_row[i][0] < lx - 90:
                            i += 1          # this number belongs further left
                        if i < len(on_row) and abs(on_row[i][0] - lx) <= 90:
                            stats[key] = int(on_row[i][1])
                            i += 1
                if len(stats) < 5:
                    self.log(f"[CAREER END] {len(stats)} of 5 stats read. "
                             f"labels: {[(k, int(lx)) for lx, _l, k in labels]} "
                             f"numbers: {[(int(c), v) for c, v in on_row]}")
            if stats:
                self._stats = stats
            m = re.search(r"RACES\D{0,4}(\d+)\D{1,8}WINS\D{0,4}(\d+)", txt)
            if m:
                self._races, self._wins = int(m.group(1)), int(m.group(2))
            m = re.search(r"FANS\s*(?:EARNED)?\D{0,4}([\d,]{3,11})", txt)
            if m:
                self._fans = int(m.group(1).replace(",", ""))
            if stats or getattr(self, "_fans", 0):
                self._shot("career_end", note=f"stats {stats} fans {getattr(self, '_fans', 0)} "
                                              f"grade {getattr(self, '_grade', '?')}")
                self.log(f"[CAREER END] stats {stats} fans {getattr(self, '_fans', 0)} "
                         f"races {getattr(self, '_races', 0)}/{getattr(self, '_wins', 0)} wins "
                         f"grade {getattr(self, '_grade', '?')}")
        except Exception as e:
            self.log(f"[BOT] career-end capture failed: {e}")

    def _add_history(self):
        """Append this career to history.json (trainee + rating, best
        effort - blank when OCR never saw them)."""
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M"),
                "n": self.careers_done,
                "trainee": getattr(self, "_trainee", "") or "",
                "rating": getattr(self, "_rating", 0) or 0,
                "stats": getattr(self, "_stats", {}) or {},
                "fans": getattr(self, "_fans", 0) or 0,
                "races": getattr(self, "_races", 0) or 0,
                "wins": getattr(self, "_wins", 0) or 0,
                "sparks": getattr(self, "_kept_sparks", "") or "",
            }
            try:
                with open("history.json", encoding="utf-8") as f:
                    hist = json.load(f)
            except Exception:
                hist = []
            hist.append(entry)
            hist = hist[-300:]
            with open("history.json", "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False, indent=1)
        except Exception as e:
            self.log(f"[BOT] history write failed: {e}")

    def _game_package(self):
        """The game's package name, looked up once from the device."""
        if hasattr(self, "_pkg"):
            return self._pkg
        self._pkg = None
        try:
            out = self.adb._run(["shell", "pm", "list", "packages"], timeout=20) or ""
            for line in out.splitlines():
                p = line.strip().replace("package:", "")
                if "umamusume" in p.lower() or "uma_gl" in p.lower():
                    self._pkg = p
                    self.log(f"[BOT] game package: {p}")
                    break
        except Exception:
            pass
        return self._pkg

    def _launch_game(self):
        """Start the game directly. More reliable than tapping the icon, and
        it works even when the launcher is on another display."""
        pkg = self._game_package()
        if not pkg:
            return False
        try:
            self.adb._run(["shell", "monkey", "-p", pkg,
                           "-c", "android.intent.category.LAUNCHER", "1"], timeout=25)
            self.log(f"[BOT] launched {pkg}")
            return True
        except Exception as e:
            self.log(f"[BOT] could not launch the game: {e}")
            return False

    def _recover_frozen_game(self, img, txt):
        """The picture stopped changing: close the game and start it again.
        Three restarts inside half an hour means it is not going to recover,
        so stop rather than burn the day."""
        now = time.time()
        n = getattr(self, "_freeze_restarts", 0) + 1
        if now - getattr(self, "_last_freeze_restart", 0) > 1800:
            n = 1                       # a calm half hour: start counting again
        self._freeze_restarts = n
        self._last_freeze_restart = now
        self.log(f"[RECOVER] the screen has not changed for "
                 f"{FREEZE_SECONDS//60} minutes - the game looks frozen "
                 f"(restart {n} of 3); screen: {txt[:100]}")
        self._shot("frozen", img, note=txt[:160])
        pkg = self._game_package()
        if n > 3 or not pkg:
            if not pkg:
                self.log("[RECOVER] cannot find the game package - stopping")
            else:
                self.log("[RECOVER] restarting did not help - stopping the bot")
            self._stop.set()
            return
        try:
            self.adb._run(["shell", "am", "force-stop", pkg], timeout=25)
            self.log(f"[RECOVER] closed {pkg}")
        except Exception as e:
            self.log(f"[RECOVER] could not close the game: {e}")
        time.sleep(3)
        self._launch_game()
        self._frozen_txt = None
        self._frozen_since = time.time()
        self.adb.repeat_taps = 0
        self._sleep(FREEZE_RELAUNCH_WAIT)   # splash + title screen

    def _game_running(self):
        pkg = self._game_package()
        if not pkg:
            return None
        try:
            out = self.adb._run(["shell", "pidof", pkg], timeout=15) or ""
            return bool(out.strip())
        except Exception:
            return None

    def _pick_game_display(self, reason=""):
        """Capture each display and keep the one that is NOT the emulator
        launcher. MuMu Nx puts apps on separate virtual displays, so the
        first id (the launcher) is usually the wrong one."""
        ids = self.adb.list_displays()
        if len(ids) < 2:
            return False
        self.log(f"[ADB] {len(ids)} displays - looking for the one showing the game"
                 + (f" ({reason})" if reason else ""))
        launcher_marks = ("MUMU STORE", "APP CLONER", "SEARCH GAMES")
        best = None
        for did in ids:
            self.adb.set_display(did)
            im = self.adb.screenshot()
            if im is None:
                continue
            txt = _all_text(ocr_boxes(im))
            if not txt.strip():
                continue
            if any(m in txt for m in launcher_marks):
                self.log(f"[ADB]   display {did}: emulator launcher")
                continue
            self.log(f"[ADB]   display {did}: game content -> using this one")
            best = did
            break
        if best:
            self.adb.set_display(best)
            return True
        self.adb.set_display(ids[0])
        return False

    def _new_career_flags(self):
        self._trainee = ""
        self._rating = 0
        self._stats = {}
        self._fans = 0
        self._races = 0
        self._wins = 0
        self._kept_sparks = ""
        self._borrow_retry_done = False
        self._budget = None
        self._cheapest_seen = 0
        self._skill_rounds = 0
        self._reroll_attempts = 0
        self._reroll_popup_taps = 0
        self._reroll_pressed = False
        self._carousel_wait = 0
        self._carousel_flips = 0
        self._carousel_logged = False
        self._sel_next_taps = 0
        self._spark_a = None
        self._spark_b = None
        self._borrow_done = False
        self._skills_done = False
        self._rerolled = False
        self._session_started = False
        self._agenda_done = False
        self._agenda_checked = False
        self._focus_done = False

    # ---- main loop -------------------------------------------------------
    def _run(self):
        self.log(f"[BOT] BatiBot v{VERSION} starting")
        self.state = "connecting"
        if not self.adb.connect():
            self.log("[BOT] cannot connect to emulator - check ADB address in settings")
            self.state = "error: adb"
            return
        img = self.adb.screenshot()
        if img is None:
            devs = ""
            try:
                devs = self.adb._run(["devices"], timeout=10).strip().replace("\n", " ")
            except Exception:
                pass
            self.log(f"[BOT] no screenshot from {self.adb.address}. adb says: {devs or '(nothing)'}")
            self.log("[BOT] if the device is missing or 'offline', restart the emulator; "
                     "if it is listed as 'device', tell Claude - the screenshot method needs changing.")
            self.state = "error: screenshot"
            return
        h, w = img.shape[:2]
        if (w, h) != (720, 1280):
            self.log(f"[BOT] note: working at {w}x{h} via auto-scaling. "
                     f"720x1280 (DPI 240) is still the recommended MuMu setting.")
        # MuMu Nx runs apps on their own virtual displays: choose the one
        # showing the game up front rather than after failed launches
        self._pick_game_display("startup")
        self._new_career_flags()

        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self.log(f"[BOT] tick error: {e}")
                time.sleep(3)
        self.state = "stopped"
        self.log("[BOT] stopped")

    def _tick(self):
        img = self.adb.screenshot()
        if img is None:
            time.sleep(4)
            return
        boxes = ocr_boxes(img)
        txt = _all_text(boxes)

        # ---- watchdog: a hung game keeps its process alive, so the crash
        # guard (pidof) is happy while nothing on screen ever changes. On the
        # 01/08 run the watch screen read "0:48:43 LEFT" for five and a half
        # hours. If the text does not change at all, restart the game.
        now = time.time()
        if txt and txt == getattr(self, "_frozen_txt", None):
            if now - getattr(self, "_frozen_since", now) > FREEZE_SECONDS:
                self._recover_frozen_game(img, txt)
                return
        else:
            self._frozen_txt = txt
            self._frozen_since = now

        # ---- watchdog: the same tap over and over means something invisible
        # is blocking us (e.g. a dialog we do not know). 23:00 run pressed
        # "Start Career" 350+ times over 90 minutes. Escape instead.
        rep = getattr(self.adb, "repeat_taps", 0)
        if rep and rep % 8 == 0:
            esc = [(360, 835), (360, 1180), (60, 1180), (517, 833), NEUTRAL_TAP]
            p = esc[(rep // 8 - 1) % len(esc)]
            self.log(f"[STUCK] same tap x{rep} - escape tap {p}; screen: {txt[:120]}")
            self._shot("STUCK", img, note=f"tap x{rep}; screen: {txt[:160]}")
            try:
                cv2.imwrite("logs/stuck_screen.png", img)
            except Exception:
                pass
            self.adb.repeat_taps = rep + 1      # do not re-fire immediately
            self.adb.tap(*p, "stuck escape")
            time.sleep(2.5)
            return
        if rep >= 60:
            self.log("[STUCK] still stuck after many escapes - stopping so nothing is wasted")
            self._stop.set()
            return

        # ---- emulator home screen: the game is not running -> launch it -----
        if ("MUMU STORE" in txt or "APP CLONER" in txt or "SEARCH GAMES" in txt) \
                and "DELETE DATA" not in txt:
            self._set_state("emulator home")
            tries = getattr(self, "_launch_tries", 0) + 1
            self._launch_tries = tries
            if self._pick_game_display("launcher visible - checking the other displays"):
                return          # found the game on another display
            if self._launch_game():
                time.sleep(25)          # splash + title take a while
                return
            p = _find(boxes, "Umamusume", 78)
            if p:
                self.log("[BOT] game is not running - launching Umamusume")
                self.adb.tap(*p, "Umamusume icon")
                time.sleep(25)          # splash + title take a while
            else:
                self.log("[BOT] on the emulator home screen but the Umamusume "
                         "icon is not visible - open the game manually")
                time.sleep(10)
            return

        # ---- title screen after launch ---------------------------------------
        if "TAP TO START" in txt or "TAP HERE TO DISPLAY" in txt:
            self._set_state("title screen")
            self.log("[BOT] title screen - tapping to start")
            self.adb.tap(360, 640, "tap to start")
            time.sleep(6)
            return

        # ---- 0. hard stops -------------------------------------------------
        if "RECOVER TP" in txt or ("TP" in txt and "NOT ENOUGH" in txt):
            if self.s.get("recover_tp", False):
                self._handle_recover_tp(boxes, txt)
                return
            self.log("[BOT] out of TP - stopping (TP recovery is OFF in settings). Recover TP and press Start again.")
            self._tap_text(boxes, "Cancel") or self.adb.tap(*POPUP_CANCEL, "close TP dialog")
            self._stop.set()
            return
        # "You need N more TP to (start a Career / reroll Sparks). Restore TP?"
        # No (201,833) / Restore (517,833). OCR often splits "Restore" and
        # "TP?" into separate boxes, so match on "MORE TP" + context too.
        if ("RESTORE TP" in txt or ("MORE TP" in txt and "RESTORE" in txt)
                or ("MORE TP" in txt and "REROLL" in txt)):
            self._set_state("restore TP prompt")
            if self.s.get("recover_tp", False):
                self.log("[TP] short on TP - pressing Restore")
                self.adb.tap(517, 833, "Restore")
            elif "REROLL" in txt:
                self.log("[TP] short on TP for the reroll and TP refill is OFF - skipping reroll")
                self._reroll_attempts = 99   # stop trying this career
                self.adb.tap(201, 833, "No")
            else:
                self.log("[TP] out of TP for the next career and TP refill is OFF - stopping.")
                self.adb.tap(201, 833, "No")
                self._stop.set()
            time.sleep(2.5)
            return
        # "This support card is already in your deck" style confirmations that
        # sit over Start Career (23:00 run: the borrow tap landed on a
        # Duplicate Support card and the dialog blocked the start silently)
        if ("DUPLICATE" in txt or "ALREADY" in txt) and _has(boxes, "Cancel", 88):
            self._set_state("duplicate support dialog")
            self.log("[BORROW] duplicate-support dialog - cancelling and re-picking the card")
            p = _find(boxes, "Cancel", 88, y_min=700) or (201, 833)
            self.adb.tap(*p, "Cancel (duplicate support)")
            self._borrow_done = False       # pick again
            time.sleep(2.5)
            return
        if "ACCOUNT ACTIVITY" in txt:
            self.log("[BOT] !!! Account activity warning on screen - STOPPING IMMEDIATELY !!!")
            self._stop.set()
            return

        # ---- 1. IT status popup (has Delete Data + title) -------------------
        if "INDEPENDENT TRAINING" in txt and "DELETE DATA" in txt:
            self._handle_status_popup(img, boxes)
            return

        # ---- 2. Final Confirmation ------------------------------------------
        if "FINAL CONFIRMATION" in txt:
            self._set_state("final confirmation")
            tab = _find(boxes, "Independent Training", y_max=300)
            if tab:
                self.adb.tap(*tab, "Independent Training tab")
            else:
                self.adb.tap(*IT_TAB, "Independent Training tab (fixed)")
            time.sleep(1.5)
            if self.s.get("agenda_name", "").strip() and not self._agenda_done:
                self._load_agenda()
                self._agenda_done = True
                return  # next tick re-detects Final Confirmation and starts
            # verify the agenda actually took: Final Confirmation shows
            # "Scheduled N" - 0 means the load silently failed
            if self._agenda_done and not getattr(self, "_agenda_checked", False):
                self._agenda_checked = True
                m = re.search(r"SCHEDULED\D{0,4}(\d{1,2})", txt)
                if m:
                    n = int(m.group(1))
                    if n > 0:
                        self.log(f"[AGENDA] verified - {n} races scheduled")
                    else:
                        self._shot("agenda_zero", img, note="Scheduled read as 0")
                        self.log("[AGENDA] 0 races scheduled - retrying the load once")
                        self._agenda_done = False
                        return
                else:
                    self.log("[AGENDA] could not read the Scheduled count")

            # Training Focus also resets every career - click the user's pick
            focus = (self.s.get("it_focus") or "").strip()
            if focus and not self._focus_done:
                self._focus_done = True
                p = _find(boxes, focus, 85, y_min=380, y_max=760)
                if p:
                    self.log(f"[FOCUS] selecting Training Focus '{focus}'")
                    self.adb.tap(*p, f"Training Focus {focus}")
                    time.sleep(1.5)
                    return  # re-detect and Start on the next tick
                self.log(f"[FOCUS] '{focus}' not visible on Final Confirmation - leaving as is")
            self.adb.tap(*IT_START, "Start!")
            time.sleep(6)
            img2 = self.adb.screenshot()
            if img2 is not None and "FINAL CONFIRMATION" in _all_text(ocr_boxes(img2)):
                self.adb.tap(*IT_START, "Start! (retry)")
                time.sleep(6)
            self._session_started = True
            self.log("[BOT] IT session started - first status check in 4 min")
            self._sleep(240)
            return

        # ---- 3. watch view / its menu ---------------------------------------
        if "GIVE UP" in txt and "TO HOME" in txt:
            self._set_state("watch menu")
            p = _find(boxes, "To Home")
            if p:
                self.adb.tap(*p, "To Home")
            time.sleep(4)
            return
        if "TIME LEFT" in txt and "DELETE DATA" not in txt and "MENU" not in txt:
            # watch view: the timer is right there, so just sleep on it. The
            # old code tapped the burger Menu hoping for "To Home" - on the
            # 31/07 run that tap never landed and it looped for 9 minutes.
            self._set_state("watch view")
            m = TIMER_RE.search(txt)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                if 60 <= secs <= 4 * 3600:
                    nap = min(secs + 30, SLEEP_CHUNK)
                    self.log(f"[BOT] training on the watch screen, {secs}s left "
                             f"- sleeping {nap}s")
                    self._sleep(nap)
                    return
            taps = getattr(self, "_watch_menu_taps", 0) + 1
            self._watch_menu_taps = taps
            if taps <= 3:
                self.adb.tap(645, 1230, "watch Menu")
            else:
                self.log("[BOT] cannot leave the watch screen - pressing Back")
                self.adb.tap(60, 1180, "Back (watch screen)")
                self._watch_menu_taps = 0
            time.sleep(2.5)
            return

        # ---- 4. skill screen -------------------------------------------------
        if ("LEARN SKILLS" in txt or ("SKILL" in txt and "RESET" in txt)) and _has(boxes, "Confirm"):
            self._handle_skills(img, boxes)
            return

        # ---- 5. sparks --------------------------------------------------------
        # ---- 5. sparks (MuMu-style: fixed coords, explicit screens) -----------
        #  C. rerolled-set carousel -> pick the starrier page
        #  A. confirm-reroll popup (Cancel + "Reroll Sparks" text): the
        #     title is ALSO "Reroll Sparks" - tap the button BESIDE Cancel,
        #     never the title (22:33 run tapped the title at (377,1106))
        #  B. "Keep this set?" popup while we still want to reroll -> Cancel
        #  D. sparks list -> Reroll (205,1178) or Confirm (513,1178)
        # "Sparks Rerolled" TITLE screen (title sits at the very top) - this
        # is a list like the sparks screen, so score it here, where the row
        # geometry works. MuMu does the same; scoring on the carousel screen
        # (BatiBot <=v0.52) returned 0 for both sets.
        if any(fuzz.partial_ratio(t.upper(), "REROLLED") >= 90 and cy < 115
               for t, cx, cy, *_ in boxes):
            self._set_state("rerolled set")
            self._rerolled = True
            if getattr(self, "_spark_b", None) is None:
                rows = self._scan_spark_set(img, boxes)
                sb, bb, db = self._score_spark_set(rows)
                self._spark_b = (sb, db)
                self._shot("sparks_rerolled", note=f"blue={bb}* score={sb:.0f} :: {db}")
                self.log(f"[SPARKS] rerolled set: blue={bb}* score={sb:.0f} ({db or 'nothing scored'})")
            self.adb.tap(360, 1178, "Sparks Rerolled - Next")
            time.sleep(2.5)
            return
        # carousel FIRST: the page LABEL ("Original Sparks" / "Rerolled
        # Sparks") sits just under the header. The carousel screen ALSO
        # carries the "Select which Sparks to keep." line, so checking that
        # text first made the bot tap Next forever (21:45 run, 30+ taps).
        page_lbl = next((t.upper() for t, cx, cy, *_ in boxes
                         if 110 < cy < 260 and "SPARK" in t.upper()
                         and ("ORIGINAL" in t.upper() or "REROLL" in t.upper())), None)
        if page_lbl is not None:
            self._handle_spark_carousel_page(page_lbl)
            return
        if "SELECT WHICH SPARKS" in txt:
            self._set_state("spark selection popup")
            n = getattr(self, "_sel_next_taps", 0) + 1
            self._sel_next_taps = n
            if n <= 4:
                self.adb.tap(360, 833, "Spark Selection - Next")
            else:
                # popup not clearing - it is probably the carousel underneath
                self.log("[SPARKS] Next not clearing the selection popup - confirming")
                self.adb.tap(360, 1182, "Spark Selection Confirm")
            time.sleep(2.5)
            return
        cancel_p = _find(boxes, "Cancel", 90)
        reroll_txts = [(cx, cy) for t, cx, cy, *_ in boxes
                       if fuzz.ratio(t.upper().strip(), "REROLL SPARKS") >= 80]
        if ("CONSUMES 30 TP" in txt or "KEEP THIS SET OF SPARKS" in txt
                or "SPARK SELECTION" in txt or reroll_txts):
            want = self.s.get("auto_reroll", False) and not self._rerolled
            if cancel_p and reroll_txts:                            # A
                self._set_state("reroll confirm popup")
                taps = getattr(self, "_reroll_popup_taps", 0)
                if want and taps < 3:
                    self._reroll_popup_taps = taps + 1
                    cy = cancel_p[1]
                    near = [p for p in reroll_txts
                            if abs(p[1] - cy) < 60 and p[0] > 360]
                    px, py = near[0] if near else (517, cy)
                    self.log("[SPARKS] confirm-reroll popup - pressing Reroll (30 TP)")
                    self._reroll_pressed = True
                    self.adb.tap(px, py, "Reroll Sparks (popup)")
                else:
                    self.log("[SPARKS] confirm-reroll popup - cancelling")
                    self.adb.tap(*cancel_p, "Cancel")
                time.sleep(2.5)
                return
            if "KEEP THIS SET OF SPARKS" in txt and cancel_p and want:  # B
                self._set_state("sparks keep popup")
                self.log("[SPARKS] keep-this-set popup - cancelling, still want to reroll")
                self.adb.tap(*cancel_p, "Cancel")
                time.sleep(2.5)
                return
            self._set_state("sparks list")                          # D
            # 30 TP already spent? then the list we now see IS the new set
            # mid-animation - wait for the comparison screen instead of
            # pressing Reroll again (last night: double rerolls, 60 TP)
            if want and getattr(self, "_reroll_pressed", False):
                w = getattr(self, "_carousel_wait", 0) + 1
                self._carousel_wait = w
                if w <= 5:
                    self.log("[SPARKS] reroll paid - waiting for the set comparison (%d/5)" % w)
                    time.sleep(2.5)
                    return
                self.log("[SPARKS] comparison never showed - confirming what's on screen")
                self._rerolled = True
                self.adb.tap(513, 1178, "Confirm")
                time.sleep(2.5)
                return
            attempts = getattr(self, "_reroll_attempts", 0)
            if want and attempts == 0:
                rows_a = self._spark_rows(img, boxes)
                if not rows_a:
                    # MuMu rule: unreadable rows -> do NOT spend 30 TP blind
                    self.log("[SPARKS] could not read the spark rows - confirming without reroll")
                    self._rerolled = True
                    self.adb.tap(513, 1178, "Confirm")
                    time.sleep(2.5)
                    return
                if self._spark_a is None:
                    full = self._scan_spark_set(img, boxes)
                    sa, ba, da = self._score_spark_set(full)
                    self._spark_a = (sa, da)
                    self._blue_a = ba
                    self._shot("sparks_original", note=f"blue={ba}* score={sa:.0f} :: {da}")
                    self.log(f"[SPARKS] original set: blue={ba}* score={sa:.0f} ({da or 'nothing scored'})")
                # judge on the WHOLE set, not just the rows that happen to be
                # visible - the blue spark can be scrolled out of view
                b_best = max(getattr(self, "_blue_a", 0),
                             max([st for k, _, st in rows_a if k == "blue"], default=0))
                if b_best >= 3:
                    self.log(f"[SPARKS] blue spark already {b_best}* - keeping this set, saving 30 TP")
                    self._kept_sparks = self._score_spark_set(self._spark_rows(img, boxes))[2]
                    self._rerolled = True
                    self.adb.tap(513, 1178, "Confirm")
                    time.sleep(2.5)
                    return
            if want and attempts < 2:
                self._reroll_attempts = attempts + 1
                self.log("[SPARKS] pressing Reroll Sparks (attempt %d/2)" % self._reroll_attempts)
                self.adb.tap(205, 1178, "Reroll Sparks")
            else:
                if want:
                    self.log("[SPARKS] reroll didn't take - confirming original set (debug shot saved)")
                    self._shot("reroll_gave_up", img)
                    try:
                        cv2.imwrite("logs/sparks_noreroll.png", img)
                    except Exception:
                        pass
                else:
                    self.log("[SPARKS] confirming sparks")
                self.adb.tap(513, 1178, "Confirm")
            time.sleep(2.5)
            return

        # ---- 6a. "Finish this Career playthrough?" confirmation ----------------
        # (dialog title is also "Complete Career" - must be checked FIRST,
        # and the Finish button lives at the bottom, Cancel to its left)
        # NOTE: the HOME screen has a "Before Career playthrough Completion"
        # bubble - "PLAYTHROUGH" alone is NOT enough to identify this dialog
        if "FINISH THIS CAREER" in txt or "LOSE ANY UNUSED SKILL" in txt:
            self._set_state("finish confirm")
            p = _find(boxes, "Finish", 88, y_min=850)
            if p:
                self.adb.tap(*p, "Finish")
            else:
                self.adb.tap(517, 917, "Finish (fixed)")
            time.sleep(4)
            return

        # ---- 6. career-end main menu ------------------------------------------
        if _has(boxes, "Complete Career", 82):
            self._set_state("career end menu")
            # this screen shows Fans + the stat block too - fill any gaps
            if len(getattr(self, "_stats", {}) or {}) < 5:
                self._capture_training_log(boxes, txt)
            # leftover check: the Skills bubble shows "Skill Pts NNN". If a
            # buy round left >=60 SP on the table, go back in (max 3 rounds).
            m = re.search(r"SKILL\s*PTS\D{0,3}(\d{2,5})", txt)
            leftover = int(m.group(1)) if m else None
            # a round costs ~4 minutes, so only go back in if the leftover can
            # actually buy something: use the cheapest price the last scan saw
            floor = max(60, getattr(self, "_cheapest_seen", 0) or 60)
            if (self._skills_done and leftover is not None and leftover >= floor
                    and getattr(self, "_skill_rounds", 0) < 3):
                self.log(f"[BOT] {leftover} SP still unspent (cheapest seen {floor}) "
                         f"- opening skills again (round {self._skill_rounds + 1}/3)")
                self._skills_done = False
            elif self._skills_done and leftover is not None and 0 < leftover < floor:
                self.log(f"[BOT] {leftover} SP left, cheapest skill seen costs {floor} "
                         f"- nothing worth buying, completing the career")
            if not self._skills_done and (self.s.get("skills", []) or self.s.get("spend_all_sp", True) or self.s.get("smart_skills", True)):
                p = _find(boxes, "Skill Pts", 80, y_min=700)
                if p:
                    self._skill_rounds = getattr(self, "_skill_rounds", 0) + 1
                    self.adb.tap(*p, "open skills")
                    time.sleep(3)
                    return
                self.log("[BOT] couldn't find Skill Pts button - completing without skill buys")
                self._skills_done = True
            p = _find(boxes, "Complete Career", 82, y_min=700)
            if p:
                self.adb.tap(*p, "Complete Career!")
            else:
                self.adb.tap(508, 1040, "Complete Career! (fixed)")
            time.sleep(3)
            return

        # ---- 7. Career Complete dialog -> home, count career -------------------
        if "CAREER COMPLETE" in txt and _has(boxes, "To Home"):
            self._set_state("career complete")
            self._tap_text(boxes, "To Home")
            self.careers_done += 1
            self.log(f"[BOT] === career #{self.careers_done} complete ===")
            self._shot("career_complete", img,
                       note=f"#{self.careers_done} {getattr(self, '_trainee', '')} "
                            f"rating {getattr(self, '_rating', 0)} grade {getattr(self, '_grade', '?')}")
            self._add_history()
            self._new_career_flags()
            maxc = int(self.s.get("max_careers", 0) or 0)
            if maxc and self.careers_done >= maxc:
                self.log(f"[BOT] reached max careers ({maxc}) - stopping")
                self._stop.set()
            time.sleep(5)
            return

        # ---- 8. borrow card list ------------------------------------------------
        if "BORROW CARD" in txt or ("FOLLOW" in txt and "SUPPORT" in txt and "RENTAL" in txt):
            self._handle_borrow(img, boxes)
            return

        # ---- 9. setup screens -----------------------------------------------------
        if "SCENARIO SELECT" in txt:
            self._set_state("scenario select")
            self._tap_text(boxes, "Next") or self.adb.tap(360, 1077, "Next (fixed)")
            time.sleep(2.5)
            return
        if "SUPPORT FORMATION" in txt or _has(boxes, "Start Career", 85):
            self._set_state("support formation")
            if not self._borrow_done:
                self.adb.tap(*GUEST_SLOT, "guest slot")
                time.sleep(2.5)
                return
            self._tap_text(boxes, "Start Career") or self.adb.tap(540, 1110, "Start Career (fixed)")
            time.sleep(3)
            return
        for setup in ("TRAINEE SELECT", "LEGACY", "SELECT TRAINEE"):
            if setup in txt and _has(boxes, "Next"):
                self._set_state("setup: " + setup.lower())
                self._tap_text(boxes, "Next")
                time.sleep(2.5)
                return

        # ---- 9b. Training Log / IT results pages -> OK -------------------------
        # contains "Career Record" + "Training", which would fool the home
        # detector below into tapping CAREER forever
        if "TRAINING LOG" in txt or "INDEPENDENT TRAINING RESULTS" in txt:
            self._set_state("training log")
            self._capture_training_log(boxes, txt)
            p = _find(boxes, "OK", 95, y_min=900)
            if p:
                self.adb.tap(*p, "OK (training log)")
            else:
                self.adb.tap(360, 1010, "OK (training log, fixed)")
            time.sleep(3)
            return

        # ---- 9b2. event REWARDS screen (Wings of Steam and Steel etc.) -------
        # its "Next" button is at the bottom, but the reward icon carries a
        # red "NEXT" ribbon badge that the generic matcher grabbed instead
        # (09:05 run: tapped (90,837) eight times until the watchdog fired)
        if "EVENT POINTS OBTAINED" in txt or ("REWARDS" in txt and "EVENT PTS" in txt):
            self._set_state("event rewards")
            p = _find(boxes, "Next", 90, y_min=1100)
            self.log("[SWEEP] event rewards screen - Next")
            self.adb.tap(*(p or (360, 1179)), "Next (event rewards)")
            time.sleep(2.5)
            return

        # ---- 9c. overnight popups (login bonus / celebrations / gifts) --------
        # These appear at the daily reset and sit over everything; they
        # dismiss on a tap (or a Close/OK button). Last night one stalled
        # the loop for 2.4h.
        if any(k in txt for k in ("LOGIN BONUS", "CELEBRATION", "ANNIVERS",
                                  "OBTAINED THE FOLLOWING", "PRESENT")) \
                and "DELETE DATA" not in txt:
            self._set_state("event popup")
            self.log("[SWEEP] login-bonus/celebration popup - dismissing")
            if not (self._tap_text(boxes, "Close", y_min=700) or self._tap_text(boxes, "OK", y_min=700)):
                self.adb.tap(*NEUTRAL_TAP, "dismiss popup")
            time.sleep(2)
            return

        # ---- 10. home ----------------------------------------------------------------
        if _has(boxes, "CAREER", 90) and ("MISSIONS" in txt or "SHOP" in txt or "EVENT" in txt or "TRAINING" in txt) and "CAREER RECORD" not in txt:
            self._set_state("home")
            # b) invisible-overlay escape: if tapping CAREER changes nothing
            # for ~8 ticks, something is eating the taps - cycle escape taps
            if self._same_state_count >= 8:
                esc = [(360, 835), (360, 1180), (64, 1180), NEUTRAL_TAP]
                p = esc[self._same_state_count % len(esc)]
                self.log(f"[SWEEP] home taps not landing (x{self._same_state_count}) - escape tap {p}")
                self.adb.tap(*p, "overlay escape")
                time.sleep(2.5)
                return
            self.adb.tap(*HOME_CAREER, "CAREER")
            time.sleep(4)
            return

        # rating + grade capture (rank splash "Rating 19,199", results "A+ RANK")
        if ("RATING" in txt or "RANK" in txt) and "EVENT POINTS" not in txt:
            for t, cx, cy, *_ in boxes:
                digits = re.sub(r"[^0-9]", "", t)
                if digits and 4 <= len(digits) <= 6 and "RATING" not in t.upper():
                    if any("RATING" in b[0].upper() and abs(b[2] - cy) < 60 for b in boxes):
                        self._rating = int(digits)
                        break
            if not getattr(self, "_grade", ""):
                for t, *_rest in boxes:
                    g = t.strip().upper().replace(" ", "").replace("RANK", "")
                    # numbered U-ranks allowed (UG1..UG9): the game's rating
                    # table has 80 tiers above SS+, far more than a plain
                    # UG/UG+/UF ladder, so the real names carry numbers
                    if re.fullmatch(r"(U[GFEDCBA][0-9]?|SS|S|A|B|C|D|E|F|G)\+?", g):
                        self._grade = g
                        if getattr(self, "_rating", 0):
                            self.log(f"[GRADE] screen shows '{g}' for rating {self._rating}")
                        break

        # ---- 11. generic advance -------------------------------------------------------
        # bottom button band first: labels like "Next Reward"/"Next Story" and
        # the red NEXT badge live mid-screen and must never win over the real
        # button sitting at the bottom of the screen
        for label in ALLOW_BUTTONS:
            p = _find(boxes, label, 88, y_min=1080) or _find(boxes, label, 88, y_min=780)
            if p:
                self._set_state(f"generic: {label.lower()}")
                self.adb.tap(*p, label)
                time.sleep(2)
                return

        # nothing recognized: neutral tap advances splash screens (rank, CARE COMPLETE...)
        self._set_state("unknown screen")
        # crash guard: if we have been staring at something unrecognisable for
        # a while, check whether the game is still running at all and restart
        # it if not (31/07: the game died mid-session while the bot tapped on)
        if self._same_state_count and self._same_state_count % 12 == 0:
            alive = self._game_running()
            if alive is False:
                self.log("[BOT] the game is not running any more - restarting it")
                self._shot("game_crashed", img, note=txt[:160])
                if self._launch_game():
                    time.sleep(30)
                    return
        if self._same_state_count in (3, 8):
            self.log(f"[BOT] unknown screen x{self._same_state_count}, text: {txt[:150]}")
            if self._same_state_count == 3:
                self._shot("unknown_screen", img, note=txt[:200])
        if not txt.strip():
            # OCR sees NOTHING - either a loading/black frame or we're
            # capturing the wrong display. Never tap blind on an empty
            # screen; save evidence once so it can be diagnosed.
            if self._same_state_count == 3:
                try:
                    import os
                    os.makedirs("logs", exist_ok=True)
                    cv2.imwrite("logs/unknown_screen.png", img)
                    self.log(f"[BOT] empty OCR: screen brightness={img.mean():.0f} "
                             f"(0=black). Saved logs/unknown_screen.png - if it's black, "
                             f"the ADB address points at the wrong display; try emulator-5554.")
                except Exception:
                    pass
            time.sleep(3)
            return
        self.adb.tap(*NEUTRAL_TAP, "")
        time.sleep(2.2)

    # ---- handlers ---------------------------------------------------------------
    def _tap_text(self, boxes, label, allow_reroll=False, y_min=0):
        if not allow_reroll and "REROLL" in label.upper():
            return None
        p = _find(boxes, label, y_min=y_min)
        if p:
            self.adb.tap(*p, label)
            return p
        return None

    def _handle_status_popup(self, img, boxes):
        """IT status popup: read complete / time left."""
        self._set_state("IT status popup")
        try:
            name_parts = [b[0].strip() for b in boxes
                          if b[3] > 340 and 545 < b[2] < 625
                          and "TRAINEE" not in b[0].upper()]
            if name_parts:
                self._trainee = " ".join(name_parts)[:60]
        except Exception:
            pass
        left = img[675:770, 40:330]
        orange = self._orange_pixels(left)
        left_txt = " ".join(b[0] for b in boxes if 675 <= b[2] <= 775 and b[1] < 350).upper()
        if "COMPLETE" in left_txt or orange > 400:
            self.log(f"[BOT] TRAINING COMPLETE (orange={orange}) - entering career completion")
            self.adb.tap(*POPUP_CAREER, "Career")
            time.sleep(5)
            return
        timer_txt = " ".join(b[0] for b in boxes if 690 <= b[2] <= 760 and b[1] > 400)
        m = TIMER_RE.search(timer_txt)
        if m:
            remaining = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if 0 <= remaining <= 3600:
                nap = min(remaining + 75, SLEEP_CHUNK)
                self.log(f"[BOT] training, {remaining}s left "
                         f"('{timer_txt.strip()}') - sleeping {nap}s")
                self.adb.tap(*POPUP_CANCEL, "Cancel")
                self._sleep(nap)
                return
        self.log(f"[BOT] couldn't read timer ('{timer_txt.strip()}') - retry in 2 min")
        self.adb.tap(*POPUP_CANCEL, "Cancel")
        self._sleep(120)

    @staticmethod
    def _orange_pixels(crop_bgr):
        try:
            hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array([10, 120, 150]), np.array([28, 255, 255]))
            return int((mask > 0).sum())
        except Exception:
            return 0

    def _handle_borrow(self, img, boxes):
        """Find the configured borrow card, then the backup card; refresh
        the guest list once if neither shows; first card as last resort."""
        self._set_state("borrow list")
        want = (self.s.get("borrow_name") or "").strip()
        backup = (self.s.get("borrow_backup") or "").strip()
        self._borrow_best = ("", 0)
        targets = [t for t in (want, backup) if t]
        for round_i in range(6):   # initial pass + up to 5 guest refreshes
            if not targets:
                break
            for ti, target in enumerate(targets):
                if self._borrow_sweep(target, debug=(round_i == 0 and ti == 0)):
                    return
                self._scroll_list_to_top(self.BAR_BORROW, 300, 1000, tag="BORROW")
            if round_i < 5:
                self.log(f"[BORROW] not in the list - refreshing guests ({round_i + 1}/5)")
                self.adb.tap(651, 1005, "refresh borrow list")
                time.sleep(2.5)
                im2 = self.adb.screenshot()
                b2 = ocr_boxes(im2) if im2 is not None else []
                self._tap_text(b2, "Confirm", y_min=700) or self._tap_text(b2, "OK", y_min=700)
                time.sleep(2)
        # the card comes from a follow, so it is ALWAYS in the list - a
        # total miss means something glitched. Close and retry once fresh.
        if want and not getattr(self, "_borrow_retry_done", False):
            self._borrow_retry_done = True
            self.log(f"[BORROW] '{want}' not found (closest OCR: '{self._borrow_best[0]}' "
                     f"{self._borrow_best[1]:.0f}) - closing the list and retrying once from scratch")
            self.adb.tap(360, 1182, "Close (borrow retry)")
            time.sleep(2.5)
            return  # the formation screen re-opens the list next tick
        if want:
            self.log(f"[BORROW] '{want}' still not found after the retry "
                     f"(closest OCR: '{self._borrow_best[0]}' {self._borrow_best[1]:.0f}) - taking first card")
        self._scroll_list_to_top(self.BAR_BORROW, 300, 1000, tag="BORROW")
        self._shot("borrow_fallback", note=f"wanted '{want}', closest OCR "
                                          f"'{self._borrow_best[0]}' {self._borrow_best[1]:.0f}")
        self.adb.tap(360, 300, "first borrow card (fallback)")
        self._borrow_done = True
        time.sleep(2.5)

    @staticmethod
    def _group_rows(boxes, gap=42, max_h=130):
        """Group OCR boxes into card rows by vertical gaps. Returns
        [(joined_text, centre_y), ...] - a wrapped title and its uma name
        end up in the same row string."""
        items = sorted(((cy, t) for t, cx, cy, *_ in boxes if 180 <= cy <= 1100),
                       key=lambda p: p[0])
        rows, cur, last = [], [], None
        for cy, t in items:
            if last is not None and cy - last > gap:
                rows.append(cur)
                cur = []
            cur.append((cy, t))
            last = cy
        if cur:
            rows.append(cur)
        # a borrow card row is ~145px tall; anything taller means two cards
        # merged (23:00 run: every card merged into one row, so the match was
        # right but the tap landed on the neighbouring Duplicate Support card)
        out = []
        for r in rows:
            stack = [r]
            while stack:
                g = stack.pop()
                if len(g) < 2 or (g[-1][0] - g[0][0]) <= max_h:
                    out.append(g)
                    continue
                gaps = [(g[i + 1][0] - g[i][0], i) for i in range(len(g) - 1)]
                _, cut = max(gaps)
                stack.extend([g[:cut + 1], g[cut + 1:]])
        rows = sorted(out, key=lambda g: g[0][0])
        # keep the per-line boxes too, so the caller can tap the exact line
        # that matched instead of the row's average y (which can land on the
        # neighbouring card when a row has a badge or wraps)
        return [(" ".join(t for _, t in r), int(sum(c for c, _ in r) / len(r)), r)
                for r in rows if r]

    def _borrow_sweep(self, target, debug=False):
        """One full top-to-bottom sweep for one card, using the same
        machinery as the skill scan (v0.51): scrollbar-verified bottom
        detection, settle-until-stable instead of fixed sleeps, band-limited
        OCR, and a barren-screen counter. Taps the row (or Close when the
        row is already 'Selected') and returns True when handled."""
        want_n = _norm_name(target)
        self._borrow_seen = set()
        prev_hash, same, barren = None, 0, 0
        hit_bottom = None
        for i in range(25):
            if self._stop.is_set():
                return True
            if i > 0:
                self.adb.swipe(360, 950, 360, 480, dur_ms=800)
                time.sleep(0.2)
                shot = self.adb.screenshot()
                hit_bottom = self._at_bottom(shot, self.BAR_BORROW) if shot is not None else None
                self._settle(prev=shot, timeout=1.2)
            im = self.adb.screenshot()
            if im is None:
                continue
            # rows only occupy y180-1100 - OCR that band, not the whole screen
            boxes = [(t, cx, cy + 180, x1, y1 + 180, x2, y2 + 180)
                     for t, cx, cy, x1, y1, x2, y2 in ocr_boxes(im[180:1100])]
            seen_before = len(getattr(self, "_borrow_seen", set()) or set())
            if not hasattr(self, "_borrow_seen") or self._borrow_seen is None:
                self._borrow_seen = set()
            for b in boxes:
                self._borrow_seen.add(_norm_name(b[0])[:24])
            if debug and i < 2:
                try:
                    cv2.imwrite(f"logs/borrow_scan_{i}.png", im)
                except Exception:
                    pass
                self.log("[BORROW] sweep %d sees: %s" % (i, " | ".join(
                    b[0] for b in boxes if 180 <= b[2] <= 1100)[:220]))
            # Rows are matched as a WHOLE (title + uma name), because a card
            # title alone can be tiny and symbol-heavy: "[Q≠0] Agnes Tachyon"
            # normalises to just "Q0", which no single-box rule can match
            # safely. Short titles must be confirmed by the uma name.
            chara_n = _norm_name(self.cards.get(want_n, ""))
            for rtext, rcy, rlines in self._group_rows(boxes):
                jn = _norm_name(rtext)
                if not jn:
                    continue
                hit_title = want_n in jn or fuzz.partial_ratio(jn, want_n) >= 88
                hit_chara = bool(chara_n) and (chara_n in jn
                                               or fuzz.partial_ratio(jn, chara_n) >= 88)
                if len(want_n) >= 6:
                    ok = hit_title or (hit_chara and fuzz.partial_ratio(jn, want_n) >= 78)
                else:                      # tiny title - the uma name decides
                    ok = hit_title and hit_chara
                score = max(fuzz.partial_ratio(jn, want_n),
                            fuzz.partial_ratio(jn, chara_n) if chara_n else 0)
                if score > self._borrow_best[1]:
                    self._borrow_best = (rtext[:48], score)
                if ok and any("DUPLICATE" in lt.upper() for _ly, lt in rlines):
                    self.log(f"[BORROW] skipping '{rtext[:36]}' - marked Duplicate Support")
                    continue
                if ok:
                    # tap the line that actually carries the card title/uma
                    tap_y = rcy
                    for ly, lt in rlines:
                        ln = _norm_name(lt)
                        if (want_n and want_n in ln) or (chara_n and chara_n in ln):
                            tap_y = int(ly)
                            break
                    rcy = tap_y
                    if any("SELECTED" in b[0].upper() and abs(b[2] - rcy) < 90
                           for b in boxes):
                        self.log(f"[BORROW] '{rtext[:40]}' already Selected - closing list")
                        self.adb.tap(360, 1182, "Close (borrow already selected)")
                    else:
                        self.log(f"[BORROW] match row '{rtext[:48]}' ~ '{target}'"
                                 + (f" / {self.cards.get(want_n)}" if chara_n else ""))
                        self.adb.tap(360, rcy, "borrow card")
                    self._borrow_done = True
                    time.sleep(2.5)
                    return True
            if hit_bottom is True:
                return False                       # scrollbar says end of list
            if len(self._borrow_seen) == seen_before:
                barren += 1
                if barren >= 3:
                    return False                   # 3 screens, nothing new
            else:
                barren = 0
            h = self._list_hash(im)
            if h is not None and h == prev_hash:
                same += 1
                if same >= 2:
                    return False
            else:
                same = 0
            prev_hash = h
        return False

    # words that appear on the skill screen but are NOT skill rows
    _SKILL_SCREEN_NOISE = ["CONFIRM", "RESET", "BACK", "SKILL", "PTS", "OWNED",
                           "LEARN", "FILTER", "SORT", "CLOSE", "DETAILS",
                           "FULL", "STATS", "POINTS", "HINT LVL", "OFF!"]

    @staticmethod
    def _list_hash(im):
        """Fingerprint of the visible list area (scrollbar excluded) -
        unchanged across a swipe with no new taps = bottom reached."""
        try:
            return cv2.resize(cv2.cvtColor(im[465:1030, 20:690],
                              cv2.COLOR_BGR2GRAY), (48, 40)).tobytes()
        except Exception:
            return None

    # scrollbar geometry per list (measured from real captures)
    # (x1, x2, y1, y2, mode) - the skill bar is a faint thumb on a plain
    # track (deviation works); the borrow bar is a DARK thumb on a bright
    # track that runs past card edges, so brightness is the reliable cue.
    BAR_SKILLS = (700, 716, 465, 1035, "dev")
    BAR_BORROW = (690, 696, 110, 1150, "dark")

    @staticmethod
    def _bar_ends(im, bar):
        """(thumb_start, thumb_end, track_len) for a scrollbar, or None."""
        try:
            x1, x2, y1, y2, mode = bar
            col = im[y1:y2, x1:x2]
            vals = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY).astype(np.float32).mean(axis=1)
            if mode == "dark":
                ys = np.where(vals < 200)[0]
            else:
                ys = np.where(np.abs(vals - np.median(vals)) > 18)[0]
            if len(ys) < 15:
                return None
            return int(ys.min()), int(ys.max()), len(vals)
        except Exception:
            return None

    @staticmethod
    def _at_top(im, bar, tol=25):
        e = ItBot._bar_ends(im, bar)
        return None if e is None else e[0] <= tol

    @staticmethod
    def _at_bottom(im, bar, tol=25):
        e = ItBot._bar_ends(im, bar)
        return None if e is None else e[1] >= e[2] - tol

    @staticmethod
    def _list_at_bottom(im):
        """Scrollbar check (per Bon): sample the thin scrollbar track on
        the right edge of the skill list. Returns True when the thumb
        touches the bottom, False when it doesn't, None when no thumb is
        visible (bar faded out) - caller falls back to the sweep cap."""
        try:
            col = im[465:1035, 700:716]
            vals = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY).astype(np.float32).mean(axis=1)
            dev = np.abs(vals - np.median(vals))
            ys = np.where(dev > 18)[0]
            if len(ys) < 15:
                return None
            # 25px tolerance: downscaling from a higher-resolution emulator
            # blurs the thumb ends by a couple of pixels (v0.48 at 1080p read
            # the thumb top at 16 with a 14px limit and never confirmed)
            return int(ys.max()) >= len(vals) - 25
        except Exception:
            return None

    @staticmethod
    def _list_at_top(im):
        """Mirror of _list_at_bottom: True when the scrollbar thumb sits
        against the TOP of the track, None when the bar isn't visible."""
        try:
            col = im[465:1035, 700:716]
            vals = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY).astype(np.float32).mean(axis=1)
            dev = np.abs(vals - np.median(vals))
            ys = np.where(dev > 18)[0]
            if len(ys) < 15:
                return None
            return int(ys.min()) <= 25
        except Exception:
            return None

    def _scroll_list_to_top(self, bar, y_from, y_to, tries=14, tag="LIST"):
        """Verified scroll-to-top for any list (same method as the skill
        list): drag from inside the list, read the scrollbar mid-drag while
        the thumb is visible, stop on a confirmed top or a settled frame."""
        prev, same = None, 0
        for i in range(tries):
            if self._stop.is_set():
                return False
            self.adb.swipe(360, y_from, 360, y_to, dur_ms=280)
            time.sleep(0.2)
            im = self.adb.screenshot()
            if im is not None and self._at_top(im, bar) is True:
                time.sleep(0.3)
                return True
            im2 = self._settle(prev=im, timeout=0.9) or self.adb.screenshot()
            h = self._list_hash(im2) if im2 is not None else None
            if h is not None and h == prev:
                same += 1
                if same >= 2:
                    return True
            else:
                same = 0
            prev = h
        self.log(f"[{tag}] could not confirm the top of the list")
        return False

    def _scroll_to_top(self):
        """Scroll the skill list back to the very top and VERIFY it, instead
        of firing a fixed number of flings (v0.38-0.43 bug: drags that began
        outside the list did nothing and the buy pass ran on the wrong
        viewport). Drags start deep inside the list, the scrollbar is read
        mid-drag while the thumb is still visible, and the loop also stops
        when the frame stops changing. Returns True when the top is
        confirmed."""
        prev, same = None, 0
        for i in range(14):
            if self._stop.is_set():
                return False
            self.adb.swipe(360, 560, 360, 1015, dur_ms=280)
            time.sleep(0.22)
            im = self.adb.screenshot()
            top = self._list_at_top(im) if im is not None else None
            if top is True:
                time.sleep(0.4)
                self.log(f"[SCROLL] back at top after {i + 1} drags")
                return True
            im2 = self._settle(prev=im, timeout=0.9) or self.adb.screenshot()
            h = self._list_hash(im2) if im2 is not None else None
            if h is not None and h == prev:
                same += 1
                if same >= 2:      # list stopped moving = top reached
                    self.log(f"[SCROLL] top reached (list stopped moving) after {i + 1} drags")
                    return True
            else:
                same = 0
            prev = h
        try:
            im4 = self.adb.screenshot()
            col = im4[465:1035, 700:716]
            v = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY).astype(np.float32).mean(axis=1)
            ys = np.where(np.abs(v - np.median(v)) > 18)[0]
            pos = f"thumb {int(ys.min())}-{int(ys.max())} of {len(v)}" if len(ys) else "no thumb visible"
        except Exception:
            pos = "unreadable"
        self.log(f"[SCROLL] WARNING: could not confirm the top of the skill list ({pos})")
        try:
            im3 = self.adb.screenshot()
            if im3 is not None:
                cv2.imwrite("logs/scrolltop_fail.png", im3)
        except Exception:
            pass
        return False

    def _list_boxes(self, im):
        """OCR only the skill-list band (y420-1030) instead of the whole
        screen - same rows, roughly half the OCR time per step. Box
        coordinates are returned in 720x1280 space.

        When the emulator runs above 720p the band is cropped from the
        NATIVE frame instead, so the text OCRs at full sharpness, and the
        boxes are scaled back down."""
        # native-resolution OCR (v0.48) cost 2.25x the pixels at 1080p for no
        # measured accuracy gain - 720 is what every row threshold was tuned
        # on. Set BATIBOT_NATIVE_OCR=1 to try the sharp path again.
        nat, sx, sy = self.adb.native, self.adb.sx, self.adb.sy
        if nat is not None and sy > 1.05 and os.environ.get("BATIBOT_NATIVE_OCR"):
            crop = nat[int(420 * sy):int(1030 * sy)]
            return [(t, cx / sx, cy / sy + 420, x1 / sx, y1 / sy + 420,
                     x2 / sx, y2 / sy + 420)
                    for t, cx, cy, x1, y1, x2, y2 in ocr_boxes(crop)]
        crop = im[420:1030]
        return [(t, cx, cy + 420, x1, y1 + 420, x2, y2 + 420)
                for t, cx, cy, x1, y1, x2, y2 in ocr_boxes(crop)]

    def _swipe_step_down(self):
        """One 300px down-drag. Samples the scrollbar RIGHT after the drag
        - the thumb fades out before the settle ends, which is why checking
        the settled frame almost never saw it. Returns True at list bottom,
        False mid-list, None if the bar was unreadable.

        v0.49: instead of a fixed settle, poll until the list stops moving
        (identical frames) - quicker when the list snaps fast, and it waits
        longer than the old fixed sleep when the animation lags."""
        self.adb.swipe(360, 950, 360, 650, dur_ms=900)
        time.sleep(0.2)
        im = self.adb.screenshot()
        bottom = self._list_at_bottom(im) if im is not None else None
        self._settle(prev=im)
        return bottom

    def _settle(self, prev=None, timeout=1.2):
        """Wait until two consecutive frames of the list are identical."""
        end = time.time() + timeout
        h_prev = self._list_hash(prev) if prev is not None else None
        while time.time() < end:
            time.sleep(0.12)
            im = self.adb.screenshot()
            if im is None:
                continue
            h = self._list_hash(im)
            if h is not None and h == h_prev:
                return im
            h_prev = h
        return None

    def _is_real_skill_name(self, text):
        """True when text matches a known skill from the game data - this
        rejects description fragments so they can never steal a row."""
        k = _norm_name(text)
        if len(k) < 3:
            return False
        if k in self.values:
            return True
        return any(fuzz.ratio(k, vk) >= 86 for vk in self.values)

    def _read_budget(self):
        """OCR just the Skill Points strip. Returns (points|None, frame)."""
        im = self.adb.screenshot()
        if im is None:
            return None, None
        try:
            for t, cx, cy, *_ in ocr_boxes(im[395:450, 260:700]):
                d = re.sub(r"[^0-9]", "", t)
                if d and 1 <= len(d) <= 5:
                    return int(d), im
        except Exception:
            pass
        return None, im

    def _tap_skill_plus(self, name, px, py, cost=None):
        """VERIFIED + tap (v0.20): the list row-snaps after a drag, so a
        tap can land a few px off. Tap, then check the Skill Points number
        actually dropped; if unchanged, re-locate the row on a fresh frame
        and tap once more. Returns True when the selection took.

        v0.49: the budget AFTER one tap is the budget BEFORE the next, so
        the baseline is carried forward (self._budget) instead of being
        re-OCR'd every time - half the budget reads, same verification."""
        b0 = getattr(self, "_budget", None)
        if b0 is None:
            b0, _ = self._read_budget()
        if cost and b0 is not None and cost > b0:
            # affordability is judged on the carried budget; re-read once
            # before actually declining, in case the cache went stale
            b0, _ = self._read_budget()
            self._budget = b0
        if cost and b0 is not None and cost > b0:
            self.log(f"[BOT] skipping '{name}' - cost {cost} > budget {b0}")
            return False  # can't afford - don't waste taps
        self.adb.tap(px, py, "skill +")
        time.sleep(0.55)
        b1, im = self._read_budget()
        self._budget = b1
        if b0 is None or b1 is None or b1 < b0:
            return True
        if b1 == b0 and im is not None:
            for t2, c2, px2, py2 in self._rows(im):
                if fuzz.ratio(t2.upper(), name.upper()) >= 90:
                    self.adb.tap(px2, py2, "skill + retry")
                    time.sleep(0.55)
                    b2, _ = self._read_budget()
                    self._budget = b2
                    ok = b2 is not None and b2 < b0
                    if not ok:
                        self.log(f"[BOT] could not select '{name}' (budget unchanged)")
                    return ok
        return b1 != b0

    @staticmethod
    def _skill_buttons(im):
        """Find the green + buttons on the Learn screen by colour.

        Adapted from Kisegami's Uma-Musume-Auto-Train, which anchors every
        skill row on the detected + button instead of guessing positions.
        Two big wins: the tap lands on the button we actually found, and
        rows WITHOUT a green + (already obtained, or greyed) are skipped
        automatically. Their bot template-matches an asset at 1080p; we
        detect the colour instead, so it needs no image file.
        Returns [(cx, cy), ...] top to bottom, inside the safe tap band."""
        try:
            hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array([40, 90, 120]), np.array([80, 255, 255]))
            mask[:, :600] = 0          # + buttons live on the right edge
            n, _lbl, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
            out = []
            for i in range(1, n):
                x, y, w, h, a = stats[i]
                cx, cy = cent[i]
                if (700 < a < 1800 and 0.65 < w / max(h, 1) < 1.5
                        and 620 < cx < 690 and 470 <= cy <= 1000):
                    out.append((int(cx), int(cy)))
            return sorted(out, key=lambda p: p[1])
        except Exception:
            return []

    def _rows_from_buttons(self, im, frame):
        """Build (name, cost, tap_x, tap_y) rows anchored on the + buttons.
        Verified against Bon's Learn captures: the price sits on the button
        line (x 500-640) and the name 30-100px above it (left column)."""
        rows = []
        for cx, cy in self._skill_buttons(im):
            cost = None
            for t, bx, by, x1, y1, x2, y2 in frame:
                if abs(by - cy) > 26 or not (480 < bx < 645):
                    continue
                d = re.sub(r"[^0-9]", "", t)
                if d and 1 <= len(d) <= 4 and 10 <= int(d) <= 3000 \
                   and "%" not in t and "OFF" not in t.upper():
                    cost = int(d)
                    break
            if cost is None:
                continue
            cands = [(t, by) for t, bx, by, *_ in frame
                     if x_left(bx) and 25 <= cy - by <= 105
                     and self._is_real_skill_name(t)]
            if not cands:
                continue
            name = max(cands, key=lambda c: cy - c[1])[0].strip()
            rows.append((name, cost, cx, cy))
        return rows

    def _rows(self, im):
        """Row detector used by every sweep: button-anchored first (fast,
        exact tap points, skips obtained rows), old name/cost pairing as a
        fallback when no green + is visible."""
        frame = self._list_boxes(im)
        rows = self._rows_from_buttons(im, frame)
        if rows:
            return rows
        return self._skill_rows(frame)

    def _skill_rows(self, frame):
        """Learn screen rows (from Bon's live capture): the skill NAME sits
        in the left column, and ~40-90px BELOW it is the -/cost/+ cluster
        on the right. Selecting requires tapping the green + (x~650) at the
        COST line. SAFETY (v0.19): only cost lines fully inside y470-1000
        are tappable - the visible list ends ~y1030 and Reset sits at
        (620,1081), so lower taps could wipe all selections. And the name
        must be a REAL skill from the game data - description fragments
        pairing with a cost caused double-tap deselects. Rows clipped at
        the edges are picked up on the next overlapping viewport."""
        lefts, costs = [], []
        for t, cx, cy, x1, y1, x2, y2 in frame:
            tt = t.strip()
            if 140 < cy < 1080 and x1 < 420 and len(tt) >= 3 \
               and not any(n in tt.upper() for n in ItBot._SKILL_SCREEN_NOISE):
                lefts.append((tt, cy))
            digits = re.sub(r"[^0-9]", "", t)
            if x1 >= 430 and 470 <= cy <= 1000 and digits and len(digits) <= 4 \
               and 40 <= int(digits) <= 3000 and "OFF" not in t.upper() and "%" not in t:
                costs.append((int(digits), cy))
        rows = []
        for cost, ccy in costs:
            cands = [(tt, ny) for tt, ny in lefts if 15 <= ccy - ny <= 95
                     and self._is_real_skill_name(tt)]
            if cands:
                name = max(cands, key=lambda c: ccy - c[1])[0]
                rows.append((name, cost, 650, ccy))
        return rows

    def _shot(self, name, im=None, note=""):
        """Save a debug screenshot + a note, when debug_shots is enabled
        (off by default - only Bon's install turns it on). Keeps the last
        80 files so it cannot fill the disk."""
        if not self.s.get("debug_shots"):
            return
        try:
            import glob
            import os
            d = os.path.join("logs", "shots")
            os.makedirs(d, exist_ok=True)
            if im is None:
                im = self.adb.screenshot()
            if im is None:
                return
            stamp = time.strftime("%H%M%S")
            path = os.path.join(d, f"{stamp}_{name}.png")
            cv2.imwrite(path, im)
            if note:
                with open(os.path.join(d, "notes.txt"), "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name}: {note}\n")
            files = sorted(glob.glob(os.path.join(d, "*.png")))
            for old_file in files[:-200]:
                try:
                    os.remove(old_file)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"[SHOT] could not save {name}: {e}")

    def _invalidate_budget(self):
        self._budget = None

    def _handle_skills(self, img, boxes):
        """ONE straight sweep top to bottom (per Bon, no scan protection):
        taps listed skills as they appear; with spend-all on (default),
        taps every other row too - the game only accepts what SP can
        afford, so leftover SP drains into the best-sorted skills.
        Each skill name is tapped at most once (tapping a selected row
        would DESELECT it). Then one Confirm."""
        self._set_state("skill buying")
        self._invalidate_budget()
        want = [s.strip() for s in self.s.get("skills", []) if s.strip()]
        blocked = [s.strip().upper() for s in self.s.get("skills_blocked", []) if s.strip()]
        # blocklist wins over the buy list
        for w in list(want):
            if any(fuzz.ratio(w.upper(), b) >= 88 for b in blocked):
                self.log(f"[BOT] '{w}' is in BOTH buy and blocked lists - blocking wins, removed from buys")
                want.remove(w)
        self._blocked_skills = blocked
        spend_all = bool(self.s.get("spend_all_sp", True))
        bought = set()
        tapped_extra = 0

        # tapped skill names for the whole screen visit - tapping a selected
        # row DESELECTS it, and viewports overlap between swipes, so each
        # name may only ever be tapped once (fuzzy match kills OCR variants)
        tapped_names = []
        prev_h = [None, 0]

        def _already_tapped(t):
            tu = t.upper()
            return any(fuzz.ratio(tu, p) >= 90 for p in tapped_names)

        def _is_blocked(t):
            tu = t.upper()
            return any(fuzz.ratio(tu, b) >= 88 or b in tu for b in blocked)

        def _is_wanted(t):
            tu = t.upper()
            for w in want:
                if w not in bought and (fuzz.ratio(tu, w.upper()) >= 86 or w.upper() in tu):
                    return w
            return None

        self._scroll_to_top()

        def sweep(n_swipes=20):
            """ONE straight pass, top to bottom (per Bon). Taps listed
            skills as they appear; with spend-all on, taps every other row
            too - the game only accepts what SP can afford."""
            nonlocal tapped_extra
            for sweep_i in range(n_swipes):
                if self._stop.is_set():
                    return
                hit_bottom = None
                if sweep_i > 0:
                    hit_bottom = self._swipe_step_down()
                im = self.adb.screenshot()
                if im is None:
                    continue
                before_taps = len(tapped_names)
                for t, cost, px, py in self._rows(im):
                    if _already_tapped(t):
                        continue
                    if _is_blocked(t):
                        continue
                    w = _is_wanted(t)
                    if w:
                        self.log(f"[BOT] selecting listed skill '{t}' ({cost} SP)")
                        tapped_names.append(t.upper())
                        if self._tap_skill_plus(t, px, py, cost):
                            bought.add(w)
                        else:
                            tapped_names.remove(t.upper())
                    elif spend_all:
                        tapped_names.append(t.upper())
                        if self._tap_skill_plus(t, px, py, cost):
                            tapped_extra += 1
                        else:
                            tapped_names.remove(t.upper())
                if hit_bottom is True or self._list_at_bottom(im):
                    break
                h = self._list_hash(im)
                if h is not None and h == prev_h[0] and before_taps == len(tapped_names):
                    prev_h[1] += 1
                    if prev_h[1] >= 2:
                        break
                else:
                    prev_h[1] = 0
                prev_h[0] = h

        smart = bool(self.s.get("smart_skills", True)) and bool(self.values)
        smart_done = False
        if smart and (want or spend_all):
            smart_done = self._skills_smart(want, bought, tapped_names)
            if smart_done:
                tapped_extra = len(tapped_names) - len(bought)
        if not smart_done and (want or spend_all):
            self.log("[BOT] skill screen: one straight sweep top to bottom"
                     + (" (spend-all on)" if spend_all else ""))
            sweep()
            self.log(f"[BOT] sweep done - listed {len(bought)}/{len(want)}, "
                     f"extra rows tapped {tapped_extra} (game accepted the affordable ones)")
        if tapped_extra > 0:
            bought.add("_spendall_")
        # confirm purchases (or just leave the screen if nothing selected)
        img = self.adb.screenshot()
        boxes = ocr_boxes(img) if img is not None else []
        if bought:
            self._tap_text(boxes, "Confirm", y_min=600)
            time.sleep(2)
            img = self.adb.screenshot()
            boxes = ocr_boxes(img) if img is not None else []
            self._tap_text(boxes, "Confirm", y_min=600) or self._tap_text(boxes, "OK", y_min=600) or self._tap_text(boxes, "Learn", y_min=600)
            time.sleep(3)
            # skill-get animation -> Close
            for _ in range(4):
                img = self.adb.screenshot()
                boxes = ocr_boxes(img) if img is not None else []
                if self._tap_text(boxes, "Close", y_min=600):
                    break
                self.adb.tap(*NEUTRAL_TAP, "")
                time.sleep(1.5)
        # leave skill screen
        img = self.adb.screenshot()
        boxes = ocr_boxes(img) if img is not None else []
        self._tap_text(boxes, "Back", y_min=600) or self.adb.tap(60, 1180, "Back (fixed)")
        self._skills_done = True
        time.sleep(2.5)

    def _skills_smart(self, want, bought, tapped_names):   # noqa: C901
        """Rating optimizer (umatool-style). Three steps, all straight:
        1. SCAN: one sweep down, no taps - record each row's name + its
           displayed (discounted) SP cost, and read the SP budget.
        2. COMPUTE: reserve the user's listed skills first, then greedy
           knapsack on rating-per-SP using grade values from the game's
           master data (skill_values.json).
        3. BUY: scroll back up, one sweep down tapping only the chosen
           skills. Returns False on any read failure -> caller falls back
           to the simple sweep."""
        self._set_state("skill scan")
        img = self.adb.screenshot()
        if img is None:
            return False
        boxes = ocr_boxes(img)

        # --- budget: the number nearest a "Skill P..." label ---------------
        budget = None
        labels = [(cx, cy) for t, cx, cy, *_ in boxes if "SKILL P" in t.upper()]
        for t, cx, cy, *_ in boxes:
            digits = re.sub(r"[^0-9]", "", t)
            if not digits or not (50 <= int(digits) <= 99999):
                continue
            if any(abs(cy - ly) < 45 for lx, ly in labels):
                budget = int(digits)
                break
        if budget is None:
            self.log("[BOT] smart skills: could not read SP budget - falling back to simple sweep")
            return False
        self._budget = budget
        self.log(f"[BOT] smart skills: SP budget {budget}")

        # --- scan sweep: names + displayed costs ----------------------------
        self._scroll_to_top()   # never assume the list is where we left it
        entries = []  # (text_upper, cost)
        prev_scan_h = None
        scan_same = 0
        barren = 0

        def _scanned(t):
            tu = t.upper()
            return any(fuzz.ratio(tu, e[0]) >= 90 for e in entries)

        for sweep_i in range(20):
            if self._stop.is_set():
                return False
            hit_bottom = None
            if sweep_i > 0:
                hit_bottom = self._swipe_step_down()
            im = self.adb.screenshot()
            if im is None:
                continue
            n_before = len(entries)
            for name, cost, px, py in self._rows(im):
                if not _scanned(name):
                    entries.append((name.upper(), cost))
            if hit_bottom is True or self._list_at_bottom(im):
                break
            # UAT-style content check (Kisegami): a screen whose skills are
            # ALL already recorded means the list stopped moving. They break
            # on the first duplicate; our viewports overlap on purpose, so we
            # need 3 barren screens in a row - a third safety net that works
            # even when the scrollbar is hidden and the frame keeps animating.
            if n_before == len(entries):
                barren += 1
                if barren >= 3:
                    self.log("[BOT] scan: 3 screens with no new skills - end of list")
                    break
            else:
                barren = 0
            h = self._list_hash(im)
            if h is not None and h == prev_scan_h and n_before == len(entries):
                scan_same += 1
                if scan_same >= 2:
                    break
            else:
                scan_same = 0
            prev_scan_h = h
        if len(entries) < 3:
            self.log(f"[BOT] smart skills: scan found only {len(entries)} priced rows - falling back")
            return False
        blocked = getattr(self, "_blocked_skills", [])
        if blocked:
            before = len(entries)
            entries = [(tu, c) for tu, c in entries
                       if not any(fuzz.ratio(tu, b) >= 88 or b in tu for b in blocked)]
            if before != len(entries):
                self.log(f"[BOT] smart skills: {before - len(entries)} blocked skill(s) excluded")
        self.log(f"[BOT] smart skills: scanned {len(entries)} skills with prices")
        self._shot("skills_scan", note=f"{len(entries)} rows: " +
                   "; ".join(f"{n}={c}" for n, c in entries[:14]))
        if entries:
            self._cheapest_seen = min(c for _n, c in entries)
        self.log("[BOT] scan saw: " + "; ".join(f"{n}={c}" for n, c in entries))

        # --- compute basket --------------------------------------------------
        def grade_of(text_u):
            k = _norm_name(text_u)
            v = self.values.get(k)
            if v:
                return v["g"], v["n"]
            best, best_r = None, 0
            for vk, vv in self.values.items():
                r = fuzz.ratio(k, vk)
                if r >= 90 and r > best_r:
                    best, best_r = vv, r
            if best:
                return best["g"], best["n"]
            return 150, None  # unknown skill: modest default, still spendable

        chosen = []
        hunted = []
        remaining = budget
        # 1) reserve listed skills (loudly - Bon needs to see WHY a listed
        # skill was skipped)
        for w in want:
            found = False
            for text_u, cost in entries:
                if any(c[0] == text_u for c in chosen):
                    continue
                if fuzz.ratio(text_u, w.upper()) >= 86 or w.upper() in text_u:
                    found = True
                    if cost <= remaining:
                        chosen.append((text_u, cost))
                        remaining -= cost
                        bought.add(w)
                        self.log(f"[BOT] reserved listed skill '{w}' ({cost} SP)")
                    else:
                        self.log(f"[BOT] listed skill '{w}' costs {cost} SP but only {remaining} left - skipped")
                    break
            if not found:
                self.log(f"[BOT] listed skill '{w}' was NOT seen in the scan - will hunt for it during the buy pass")
                hunted.append(w.upper())
        # 2) greedy on rating-per-SP
        rest = [(text_u, cost, grade_of(text_u)[0]) for text_u, cost in entries
                if not any(c[0] == text_u for c in chosen)]
        rest.sort(key=lambda e: e[2] / max(e[1], 1), reverse=True)
        exp_rating = sum(grade_of(c[0])[0] for c in chosen)
        for text_u, cost, grade in rest:
            if cost <= remaining:
                chosen.append((text_u, cost))
                remaining -= cost
                exp_rating += grade
        self.log(f"[BOT] smart skills: buying {len(chosen)} skills, spending "
                 f"{budget - remaining}/{budget} SP, ~{exp_rating} rating")
        if not chosen:
            return False

        # --- buy sweep: back to top, one pass, tap the chosen ---------------
        # NOTE: drags must START inside the list (y470-1030). y400 is the
        # Skill Points strip - drags there do NOTHING (v0.38 bug: the buy
        # pass ran with the list still at the bottom, tapped 0 rows).
        self._set_state("skill buying (smart)")
        self._scroll_to_top()
        chosen_names = [c[0] for c in chosen] + hunted
        prev_buy_h = None
        buy_same = 0
        for sweep_i in range(20):
            if self._stop.is_set():
                return False
            hit_bottom = None
            if sweep_i > 0:
                hit_bottom = self._swipe_step_down()
            im = self.adb.screenshot()
            if im is None:
                continue
            nb = len(tapped_names)
            rows_here = self._rows(im)
            for t, cost, px, py in rows_here:
                tt = t.strip().upper()
                if any(fuzz.ratio(tt, p) >= 90 for p in tapped_names):
                    continue
                if any(fuzz.ratio(tt, cn) >= 90 for cn in chosen_names):
                    tapped_names.append(tt)
                    if not self._tap_skill_plus(t, px, py, cost):
                        tapped_names.remove(tt)
            if sweep_i < 3:
                self.log(f"[BUYDBG] step {sweep_i}: {len(rows_here)} rows, "
                         f"{len(tapped_names) - nb} tapped, bottom={hit_bottom}")
                if sweep_i == 0 and not rows_here:
                    try:
                        cv2.imwrite("logs/buydbg0.png", im)
                    except Exception:
                        pass
            if hit_bottom is True or self._list_at_bottom(im):
                break
            h = self._list_hash(im)
            if h is not None and h == prev_buy_h and nb == len(tapped_names):
                buy_same += 1
                if buy_same >= 2:
                    break
            else:
                buy_same = 0
            prev_buy_h = h
        self.log(f"[BOT] smart buy pass done - tapped {len(tapped_names)} rows")
        if not tapped_names:
            self._shot("buy_pass_empty", note=f"chose {len(chosen)} skills but tapped none")

        # second pass for anything chosen but not yet tapped
        missing = [cn for cn in chosen_names
                   if not any(fuzz.ratio(cn, p) >= 90 for p in tapped_names)]
        # skills the scan never saw are not in this uma's pool - re-sweeping
        # for them costs ~75s and finds nothing (30/07 log: round 2 second
        # pass tapped 0 rows). Only chase things the scan actually saw.
        seen_names = [e[0] for e in entries]
        missing = [cn for cn in missing
                   if any(fuzz.ratio(cn, sn) >= 88 for sn in seen_names)]
        if missing:
            self.log(f"[BOT] {len(missing)} chosen skill(s) missed - second pass for: "
                     + ", ".join(missing[:5]))
            self._scroll_to_top()
            prev2, same2 = None, 0
            for sweep_i in range(20):
                if self._stop.is_set():
                    return False
                hit_bottom = None
                if sweep_i > 0:
                    hit_bottom = self._swipe_step_down()
                im = self.adb.screenshot()
                if im is None:
                    continue
                nb2 = len(tapped_names)
                for t, cost, px, py in self._rows(im):
                    tt = t.strip().upper()
                    if any(fuzz.ratio(tt, p) >= 90 for p in tapped_names):
                        continue
                    if any(fuzz.ratio(tt, cn) >= 90 for cn in missing):
                        tapped_names.append(tt)
                        if not self._tap_skill_plus(t, px, py, cost):
                            tapped_names.remove(tt)
                if hit_bottom is True or self._list_at_bottom(im):
                    break
                h2 = self._list_hash(im)
                if h2 is not None and h2 == prev2 and nb2 == len(tapped_names):
                    same2 += 1
                    if same2 >= 2:
                        break
                else:
                    same2 = 0
                prev2 = h2
        return len(tapped_names) > 0

    def _load_agenda(self):
        """The game resets the agenda every career. Load the user's saved
        agenda from My Agendas by name: Edit -> My Agendas -> Load List
        (button nearest below the matching name) -> Overwrite -> Close x2.
        'Save Here' is in the global blocklist and can never be clicked."""
        name = self.s.get("agenda_name", "").strip()
        self._set_state("loading agenda")
        img = self.adb.screenshot()
        boxes = ocr_boxes(img) if img is not None else []
        if not self._tap_text(boxes, "Edit"):
            self.log("[BOT] no Edit button on Final Confirmation - skipping agenda")
            return
        time.sleep(2.2)
        img = self.adb.screenshot()
        boxes = ocr_boxes(img) if img is not None else []
        if not self._tap_text(boxes, "My Agendas"):
            self.log("[BOT] My Agendas button not found - closing, career runs with goal races only")
            self._tap_text(boxes, "Close")
            time.sleep(2)
            return
        time.sleep(2.2)
        # ALWAYS load the FIRST (top) saved agenda - slot names are tiny
        # 2-3 char OCR targets and unreliable. Convention: save your IT
        # agenda in the top slot. The Overwrite check below is the gate.
        loaded = False
        img = self.adb.screenshot()
        boxes = ocr_boxes(img) if img is not None else []
        # strict whole-text match: partial_ratio scored tiny OCR fragments
        # (the "i" info icon!) as 100 because they occur inside "LOAD LIST".
        # Buttons also only exist in the slot list, below the header area.
        loads = [(cx, cy) for text, cx, cy, *_ in boxes
                 if fuzz.ratio(text.upper().strip(), "LOAD LIST") >= 80
                 and cy > 400]
        if not loads:
            # the buttons are stylised - if OCR misses them, use the known
            # position of the FIRST slot's Load List (720x1280 layout)
            self.log("[AGENDA] Load List not readable - using its fixed position")
            loads = [(601, 585)]
        if loads:
            lx, ly = min(loads, key=lambda p: p[1])  # topmost = first slot
            self.adb.tap(lx, ly, "Load List (first slot)")
            time.sleep(2.2)
            img = self.adb.screenshot()
            boxes = ocr_boxes(img) if img is not None else []
            if "OVERWRITE" in _all_text(boxes):
                self._tap_text(boxes, "Overwrite")
                self.log("[BOT] first saved agenda loaded (overwrote current schedule)")
                time.sleep(2)
            else:
                # no prompt = current schedule was empty, load was instant
                self.log("[BOT] first saved agenda loaded")
            loaded = True
        if not loaded:
            self.log("[BOT] no Load List button found in My Agendas - career runs with goal races only")
        # back out: Close (My Agendas) then Close (Agenda editor)
        for _ in range(2):
            img = self.adb.screenshot()
            boxes = ocr_boxes(img) if img is not None else []
            self._tap_text(boxes, "Close")
            time.sleep(2)

    def _handle_recover_tp(self, boxes, txt):
        """Refill TP to full (user opt-in). Mirrors the MuMu bot: TP Drink
        items first if any are owned, otherwise carats via the Max button.
        All taps are fixed coordinates - "RECOVER" stays in the blocklist
        so OCR-driven clicks elsewhere can never trigger this."""
        self._set_state("recovering TP")
        self._tp_ticks = getattr(self, "_tp_ticks", 0) + 1
        if self._tp_ticks > 15:
            self.log("[BOT] TP recovery seems stuck (not enough carats?) - stopping.")
            self._stop.set()
            return
        if "NOT ENOUGH" in txt:
            self.log("[TP] not-enough-TP prompt - opening Recover TP")
            self.adb.tap(520, 830, "Recover TP prompt")
        elif _find(boxes, "Max", 90, y_min=600, y_max=740):
            self.log("[TP] amount dialog - Max, then Confirm")
            self.adb.tap(626, 670, "Max")
            time.sleep(0.5)
            self.adb.tap(525, 920, "Confirm recovery")
        elif _has(boxes, "Close") and ("RECOVERED" in txt or "COMPLETE" in txt):
            self.log("[TP] recovery complete - closing")
            self.adb.tap(360, 835, "Close recovery result")
            self._tp_ticks = 0
        elif _find(boxes, "Confirm", 85, y_min=850):
            self.adb.tap(525, 920, "Confirm recovery")
        elif "TP DRINK" in txt and not self.s.get("recover_tp_carats_only", False):
            self.log("[TP] using a TP Drink")
            self.adb.tap(610, 320, "Use TP Drink")
        else:
            self.log("[TP] no TP Drinks - buying with carats (to full)")
            self.adb.tap(610, 180, "Use carats")
        time.sleep(2.5)

    # ---- spark scoring, ported from the MuMu bot (Bon's rules) ----------
    # blue: 3*=1000 / 2*=200 / 1*=0 (+tiny type bonus Spd>Pow>Wit>Sta>Guts)
    # pink: stars, DOUBLED for distance/track (Sprint/Mile/Medium/Long/Dirt)
    # green: stars. white: stars, +10 when the skill is in the user's buy list
    SR_BLUE = ("SPEED", "STAMINA", "POWER", "GUTS", "WIT", "WISDOM")
    SR_PINK2 = ("SPRINT", "MILE", "MEDIUM", "LONG", "DIRT")
    SR_RANK = {"SPEED": 4, "POWER": 3, "WIT": 2, "WISDOM": 2, "STAMINA": 1, "GUTS": 0}

    def _spark_rows(self, im, boxes):
        """Classify the visible spark rows.

        v0.58: rows are anchored on the OCR name boxes instead of a fixed
        y-grid (205 + 79*i). The grid is only correct while the list sits
        at the top, so after the scroll pass every sample landed between
        rows - blue sparks were being classified as white ("rows white:9,
        blue:0" in the 08:13 run, which is impossible). Bar colour is now
        sampled on the row's own line, stars at x566/598/630 of that line."""
        rows = []
        h = im.shape[0]

        def patch(cx, cy):
            p = im[max(0, cy - 3):cy + 3, max(0, cx - 3):cx + 3]
            if p.size == 0:
                return 0, 0, 0
            return (int(p[:, :, 0].mean()), int(p[:, :, 1].mean()), int(p[:, :, 2].mean()))

        # candidate row lines: left-column text boxes (the spark names)
        anchors = []
        for t, cx, cy, x1, y1, x2, y2 in (boxes or []):
            if 150 < cy < min(h - 40, 1080) and x1 < 430 and len(t.strip()) >= 3:
                if not any(abs(cy - a) < 30 for a in anchors):
                    anchors.append(int(cy))
        if not anchors:                      # fall back to the fixed grid
            anchors = [205 + i * 79 for i in range(11) if 205 + i * 79 < min(h - 40, 1060)]
        anchors.sort()

        def by_colour(cy):
            """Colour bar on the SPARKS list screen (x180). NOT sampled on
            the Spark Selection screen, where the only coloured thing on a
            row is the radio button - that made every row read as blue."""
            b, g, r = patch(180, cy)
            if b > 180 and b > r + 50 and g > 110:
                return "blue"
            if r > 200 and g < 175 and r > g + 55 and b > 120:
                return "pink"
            if g > 165 and g > r + 35 and b < 140:
                return "green"
            return None

        for cy in anchors:
            name_here = " ".join(t for t, bx, by, *_ in (boxes or [])
                                 if abs(by - cy) <= 26 and bx < 540).strip()
            kind = self._spark_kind(name_here) or by_colour(cy) or "white"
            stars = 0
            for sx in (566, 598, 630):
                sb, sg, sr = patch(sx, cy)
                if sr > 185 and sg > 140 and sb < 155:
                    stars += 1
            if stars == 0:
                continue
            if not name_here:
                continue
            rows.append((kind, name_here, stars))
        return rows

    # blue = the five stats; pink = aptitudes (track / distance / style).
    # Everything else (race names, skills, scenario sparks) is white.
    _BLUE_NAMES = ("SPEED", "STAMINA", "POWER", "GUTS", "WIT", "WISDOM")
    _PINK_NAMES = ("TURF", "DIRT", "SPRINT", "MILE", "MEDIUM", "LONG",
                   "FRONTRUNNER", "PACECHASER", "LATESURGER", "ENDCLOSER")

    @classmethod
    def _spark_kind(cls, name):
        """Row type from its NAME - works on every screen, unlike colour."""
        n = _norm_name(name)
        if not n:
            return None
        for b in cls._BLUE_NAMES:
            if n == b or fuzz.ratio(n, b) >= 88:
                return "blue"
        for p in cls._PINK_NAMES:
            if n == p or fuzz.ratio(n, p) >= 90:
                return "pink"
        return None

    def _scan_spark_set(self, im=None, boxes=None):
        """Scan one carousel page: visible rows, one scroll for the whites
        below the fold, dedupe by name."""
        rows = {}
        barren = 0
        scrolled = 0
        for pass_i in range(8):          # sets can run to 16+ sparks
            if im is None or pass_i > 0:
                im = self.adb.screenshot()
                if im is None:
                    break
                boxes = ocr_boxes(im)
            before = len(rows)
            for kind, name, stars in self._spark_rows(im, boxes):
                rows.setdefault(name.upper(), (kind, name, stars))
            added = len(rows) - before
            self.log(f"[SPARKS] scan pass {pass_i}: +{added} (total {len(rows)})")
            if pass_i and added == 0:
                barren += 1
                if barren >= 2:          # two-strike, like the skill scan:
                    break                # a single missed swipe can't end it
            else:
                barren = 0
            self.adb.swipe(360, 780, 360, 330, dur_ms=700)
            scrolled += 1
            time.sleep(0.9)
            im = None
        if len(rows) < 6:
            try:
                cv2.imwrite("logs/sparks_scan_low.png", self.adb.screenshot())
                self.log("[SPARKS] only %d rows parsed - saved logs/sparks_scan_low.png" % len(rows))
            except Exception:
                pass
        # scroll back to the top: the blue spark sits in the first rows, and
        # later checks (e.g. "is the blue already 3*?") read whatever is on
        # screen - leaving the list at the bottom would hide it
        for _ in range(scrolled + 1):
            if self._stop.is_set():
                break
            self.adb.swipe(360, 330, 360, 780, dur_ms=450)
            time.sleep(0.5)
        time.sleep(0.4)
        return list(rows.values())

    def _score_spark_set(self, rows):
        """Returns (score, best_blue_stars, description)."""
        prio = [s.upper() for s in self.s.get("skills", [])]
        score, blue_best, parts = 0.0, 0, []
        for kind, name, stars in rows:
            nu = name.upper()
            if kind == "blue":
                m = max(self.SR_BLUE, key=lambda c: fuzz.ratio(nu, c))
                m = m if fuzz.ratio(nu, m) >= 60 else ""
                pts = {1: 0, 2: 200, 3: 1000}.get(stars, 0) + self.SR_RANK.get(m, 0)
                blue_best = max(blue_best, stars)
                score += pts
                parts.append(f"blue {name} {stars}*={pts}")
            elif kind == "pink":
                dbl = any(fuzz.ratio(nu, c) >= 60 for c in self.SR_PINK2)
                pts = stars * (2 if dbl else 1)
                score += pts
                parts.append(f"pink {name} {stars}*={pts}")
            elif kind == "green":
                score += stars
            else:
                # 85, not 72: at 72 "Ignited Spirit: Speed +" earned the
                # bonus for a list holding Ignited Spirit PWR/WIT
                bonus = 10 if any(fuzz.ratio(nu, p) >= 85 for p in prio) else 0
                score += stars + bonus
                if bonus:
                    parts.append(f"white {name} {stars}*+{bonus}")
        kinds = {}
        for k, _n, _s in rows:
            kinds[k] = kinds.get(k, 0) + 1
        if rows:
            parts.append("rows " + " ".join(f"{k}:{v}" for k, v in sorted(kinds.items())))
        return score, blue_best, "; ".join(parts)

    def _handle_spark_carousel_page(self, page_lbl):
        """Carousel page picker - same logic as the MuMu bot: decide the
        winner from the two SCORED sets (original scored before the reroll,
        rerolled scored on its own screen), then flip pages until the page
        LABEL matches the winner. The MuMu bot verifies the label rather
        than assuming page order; BatiBot <=v0.52 assumed page1=original,
        which is what made it keep the wrong set."""
        self._set_state("spark carousel")
        sa, da = self._spark_a or (0, "")
        sb, db = self._spark_b or (0, "")
        want_rerolled = sb >= sa
        target = "REROLL" if want_rerolled else "ORIGINAL"
        if not getattr(self, "_carousel_logged", False):
            self._carousel_logged = True
            self.log(f"[SPARKS] keeping {'REROLLED' if want_rerolled else 'ORIGINAL'} set "
                     f"(rerolled {sb:.0f} vs original {sa:.0f})")
        flips = getattr(self, "_carousel_flips", 0)
        on_target = (target in page_lbl) or (target == "ORIGINAL" and "REROLL" not in page_lbl)
        if on_target:
            self._kept_sparks = db if want_rerolled else da
            self.adb.tap(360, 1182, "Spark Selection Confirm")
            # keep the scores: the game shows the carousel again on the
            # confirmation step, and clearing them made the second visit
            # default to "keep rerolled" and flip away from the winner
            time.sleep(2.5)
            return
        if flips >= 5:
            self.log(f"[SPARKS] could not reach the {target} page - confirming this one")
            self.adb.tap(360, 1182, "Spark Selection Confirm")
            time.sleep(2.5)
            return
        self._carousel_flips = flips + 1
        self.adb.tap(*CAROUSEL_RIGHT, f"carousel arrow (page '{page_lbl[:18]}')")
        time.sleep(1.4)
