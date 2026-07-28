# BatiBot

BatiBot loops Uma Musume **Independent Training** careers automatically on MuMu
Player. The game plays each 50-minute career by itself; this bot just
starts sessions, waits, buys your skills, finishes the career, and starts
the next one. Runs all night.

## Features

- Full hands-off loop: ~25 careers/day for fans, trophies, and spark rolls
- Reads the exact "Time Left" timer - no wasted polling, no missed endings
- Auto-loads your saved race agenda (**always the TOP My Agendas slot**)
  before every career, since the game resets the agenda each run
- Skill buying from a typo-proof picker (1,500+ skill names built in),
  then spends ALL leftover SP top-of-list so nothing is wasted (higher
  career rank)
- Borrow support card picker
- Optional one spark reroll per career (keeps the starrier set)
- Stops by itself when TP runs out. Optional "Refill TP" setting (OFF
  by default) uses your TP Drinks first, then **buys TP with carats** -
  only turn this on if you are okay spending carats
- Hard blocklist: physically cannot click Delete Data, Give Up, or
  overwrite your saved agenda
- Simple web UI, no config files to edit

## Keeping the data up to date

The skill picker, borrow-card picker and the skill scorer are built from
the game's own data. After a game update adds new skills or cards, run
**update-data.bat** once (the game must be installed on the same PC via
Steam; otherwise pass the path to `master.mdb`). Restart BatiBot after.

## Disclaimer

Automation is against Cygames' Terms of Service. This bot only presses
the same buttons a player would and uses the game's own auto-play mode,
but **use it at your own risk** - the authors take no responsibility for
account actions.

## What it does per career

1. Home -> CAREER -> clicks through career setup using your **last-used**
   trainee / legacy / deck (it changes nothing).
2. Picks the borrow support card you named in the settings (falls back to
   the first card if not found).
3. Final Confirmation -> **Independent Training** tab -> Start!
4. Sleeps ~50 minutes (reads the exact "Time Left" from the game).
5. On TRAINING COMPLETE: buys the skills from your list (one pass down the
   list), completes the career, handles sparks (optional single reroll),
   returns home, and starts the next career.
6. Stops by itself when TP runs out (it will NEVER spend carats) or when
   your "max careers" number is reached.

## Setup (once)

1. Install Python 3.10+ from python.org - tick **"Add python.exe to PATH"**.
2. Run `install.bat` (takes a few minutes - it downloads the OCR engine).
3. MuMu Player: set display to **720x1280 (portrait), DPI 240** - this
   is the tested setup. Other resolutions still work (the bot scales
   automatically) but 9:16 portrait is required. Default MuMu ADB
   port is `127.0.0.1:16384` - if you changed it, update it in the bot UI.
4. In the game, set up Independent Training ONCE by hand: Training Focus,
   Agenda, Prioritized Skills. The bot never touches these - it reuses
   whatever you set.
5. IMPORTANT - Agenda: the game RESETS your race schedule every career.
   Save it once: Agenda -> Edit -> My Agendas -> **Save Here on the TOP
   slot** (the first one in the list). Then tick "Load your FIRST saved
   agenda" in the bot settings - the bot re-loads that top slot before
   every career. The bot ALWAYS uses the TOP save slot, so keep your IT
   schedule there.

## Run

1. Start MuMu + the game, sit on the home screen (or with an IT session
   already running - the bot picks it up either way).
2. Run `Start.bat`, open http://127.0.0.1:8099
3. Fill in your borrow card name and skill list, Save, press **Start**.

## Safety

- The bot has a hard blocklist: it can never click **Delete Data**,
  **Give Up**, **Recover TP**, or **Edit Team**.
- It stops immediately if the game shows an account activity warning.
- One IT session at a time is a game rule - don't run this while you or
  another bot is mid-career on the same account.

## Troubleshooting

- "cannot connect": check MuMu is running and the ADB address. In MuMu,
  Settings -> Others -> ADB must be enabled.
- "resolution is WxH": set MuMu display to 720x1280 portrait, restart game.
- Bot idles on an "unknown screen": close any event/login popups once -
  the bot only knows career screens. Check logs/ for details.
