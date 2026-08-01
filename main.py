"""BatiBot - entry point. UI at http://127.0.0.1:8099"""
import json
import os
import time
import logging

from itbot.bot import ItBot
from itbot.server import make_app

SETTINGS_FILE = "settings.json"
DEFAULTS = {
    "adb_path": "adb",
    "adb_address": "127.0.0.1:16384",
    "borrow_name": "",
    "borrow_backup": "",
    "agenda_name": "",
    "skills": [],
    "skills_blocked": [],
    "spend_all_sp": True,
    "smart_skills": True,
    "auto_reroll": False,
    "recover_tp": False,
    "recover_tp_carats_only": False,
    "it_focus": "",
    "debug_shots": False,   # save screenshots of what the bot reads (logs/shots)
    "max_careers": 0,
    "borrow_sweeps": 6,
}

settings = dict(DEFAULTS)
if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            settings.update(json.load(f))
    except Exception:
        pass


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


logbuf = []
os.makedirs("logs", exist_ok=True)
_logfile = open(os.path.join("logs", time.strftime("run_%Y%m%d_%H%M%S.log")),
                "a", encoding="utf-8")


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    print(line, flush=True)
    logbuf.append(line)
    del logbuf[:-1000]
    try:
        _logfile.write(line + "\n")
        _logfile.flush()
    except Exception:
        pass


_bot = None


def get_bot():
    return _bot


def start_bot():
    global _bot
    if _bot and _bot.running():
        return False
    _bot = ItBot(settings, log)
    return _bot.start()


def stop_bot():
    if _bot:
        _bot.stop()


def open_ui():
    """Open the UI in its OWN window (browser app mode - no tabs, no
    address bar), so it doesn't get lost among the user's open tabs.
    Tries Edge (preinstalled on Windows), then Chrome, then a normal tab."""
    url = "http://127.0.0.1:8099"
    import subprocess
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--new-window"])
                return
            except Exception:
                continue
    import webbrowser
    webbrowser.open(url)


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    log("BatiBot - open http://127.0.0.1:8099 in your browser")
    import threading
    threading.Timer(1.5, open_ui).start()
    app = make_app(get_bot, start_bot, stop_bot, settings, save_settings, logbuf)
    app.run(host="127.0.0.1", port=8099)
