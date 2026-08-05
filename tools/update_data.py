"""BatiBot data updater - regenerates skills.json, cards.json and
skill_values.json from the game's own master.mdb.

Run this after a game update so the pickers and the skill scorer know
about new skills and support cards.

    python tools/update_data.py                (finds the Steam install)
    python tools/update_data.py "C:\\path\\to\\master.mdb"

master.mdb normally lives in:
  <Steam>\\steamapps\\common\\UmamusumePrettyDerby\\UmamusumePrettyDerby_Data\\Persistent\\master\\master.mdb
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

DEFAULTS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\UmamusumePrettyDerby\UmamusumePrettyDerby_Data\Persistent\master\master.mdb",
    r"C:\Program Files\Steam\steamapps\common\UmamusumePrettyDerby\UmamusumePrettyDerby_Data\Persistent\master\master.mdb",
    os.path.expandvars(r"%LOCALAPPDATA%Low\Cygames\umamusume\master\master.mdb"),
]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RARITY = {1: "R", 2: "SR", 3: "SSR"}


def norm(n):
    return re.sub(r"[^A-Z0-9]", "", n.upper())


def find_mdb():
    if len(sys.argv) > 1:
        return sys.argv[1]
    for p in DEFAULTS:
        if os.path.exists(p):
            return p
    return None


def main():
    mdb = find_mdb()
    if not mdb or not os.path.exists(mdb):
        print("Could not find master.mdb. Pass its full path:")
        print('   python tools/update_data.py "C:\\...\\master\\master.mdb"')
        return 1
    print("reading", mdb)
    tmp = os.path.join(tempfile.gettempdir(), "batibot_master.mdb")
    shutil.copy2(mdb, tmp)          # never open the game's live file
    db = sqlite3.connect(tmp)

    # ---- skill names (picker) ----
    names = sorted({t.strip() for _, t in db.execute(
        "select [index], text from text_data where category=47") if t.strip()},
        key=str.upper)
    write(os.path.join(ROOT, "skills.json"), names)
    print(f"skills.json      {len(names)} skill names")

    # ---- support cards (borrow picker) ----
    titles = dict(db.execute("select [index], text from text_data where category=75"))
    cards = []
    for cid, _chara, rar in db.execute("select id, chara_id, rarity from support_card_data order by id"):
        full = (titles.get(cid) or "").strip()
        if not full:
            continue
        if full.startswith("[") and "]" in full:
            name = full[1:full.index("]")]
            chara = full[full.index("]") + 1:].strip()
        else:
            name, chara = full, ""
        cards.append({"id": cid, "name": name,
                      "desc": f"{chara} ({RARITY.get(rar, rar)})"})
    write(os.path.join(ROOT, "cards.json"), cards)
    print(f"cards.json       {len(cards)} support cards")

    # ---- skill values (scorer + row validation) ----
    grades = dict(db.execute("select id, grade_value from skill_data"))
    rarity = dict(db.execute("select id, rarity from skill_data"))
    costs = dict(db.execute("select id, need_skill_point from single_mode_skill_need_point"))
    values = {}
    for sid, text in db.execute("select [index], text from text_data where category=47"):
        text = (text or "").strip()
        if not text:
            continue
        g = grades.get(sid, 0) or 0
        if g < 0:            # x-skills (negative effects) must never be bought
            continue
        key = norm(text)
        if not key:
            continue
        entry = {"n": text, "g": int(g), "c": int(costs.get(sid, 0) or 0),
                 "r": int(rarity.get(sid, 0) or 0)}
        # keep the better-graded duplicate (same name, different ids)
        if key not in values or entry["g"] > values[key]["g"]:
            values[key] = entry
    write(os.path.join(ROOT, "skill_values.json"), values)
    graded = sum(1 for v in values.values() if v["g"] > 0)
    print(f"skill_values.json {len(values)} entries ({graded} with a rating grade)")

    # ---- gold -> white upgrade pairs ----
    # Skills come in pairs that share a group_id: rarity 1 is the white
    # version, rarity 2 the gold upgrade. Buying the gold gives you the
    # white as well, and the white's row then disappears from the shop -
    # so the bot must not reserve skill points for a white whose gold it
    # is already buying.
    names_by_id = {sid: (t or "").strip()
                   for sid, t in db.execute(
                       "select [index], text from text_data where category=47")}
    by_group = {}
    for sid, rar, grp in db.execute(
            "select id, rarity, group_id from skill_data where group_id is not null"):
        nm = names_by_id.get(sid)
        if nm and rar in (1, 2):
            by_group.setdefault(grp, {})[rar] = nm
    # x-skills (the negative ones) are paired too, but their names differ
    # from the good ○ version by a single character - keeping them makes
    # fuzzy matching confuse "Right-Handed ○" with "Right-Handed ×"
    pairs = {g[2].upper(): g[1].upper()
             for g in by_group.values() if 1 in g and 2 in g
             and "×" not in g[1] and "×" not in g[2]}
    write(os.path.join(ROOT, "skill_pairs.json"), pairs)
    print(f"skill_pairs.json  {len(pairs)} gold->white upgrade pairs")

    # ---- career rank thresholds (rating -> letter grade) ----
    # ladder confirmed from the game (20,556 shows "UG2 RANK"): the 18
    # ordinary ranks, then TEN tiers per U-letter - UG, UG1..UG9, UF,
    # UF1..UF9, UE... - which matches the 98 tiers in single_mode_rank
    names = ["G", "G+", "F", "F+", "E", "E+", "D", "D+", "C", "C+", "B", "B+",
             "A", "A+", "S", "S+", "SS", "SS+"]
    for _L in "GFEDCBA":
        names.append("U" + _L)
        names += [f"U{_L}{_i}" for _i in range(1, 10)]
    ranks = []
    for i, (rid, lo, hi) in enumerate(db.execute(
            "select id, min_value, max_value from single_mode_rank order by id")):
        ranks.append([lo, hi, names[i] if i < len(names) else f"U*{i}"])
    write(os.path.join(ROOT, "ranks.json"), ranks)
    print(f"ranks.json        {len(ranks)} rank tiers")
    print("\nDone. Restart BatiBot to load the new data.")
    return 0


def write(path, obj):
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0 if isinstance(obj, list) else 1)


if __name__ == "__main__":
    sys.exit(main())
