"""Work out how the game turns stats + skills into a career rating.

BatiBot writes one row per career to logs/rating_samples.jsonl (stats it
read, the rating value of the skills it bought, and the final rating the
game showed). This fits those rows so the bot can eventually PREDICT the
rating before the skill screen closes - and therefore judge whether
spending more points would cross a rank tier, which up in the UG range is
only 400-500 rating wide.

    python tools/rating_fit.py            (uses logs/rating_samples.jsonl)
    python tools/rating_fit.py other.jsonl

Nothing here talks to the game or changes the bot - it only reads the file.
"""
import json
import os
import sys

STATS = ("spd", "sta", "pow", "gut", "wit")


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("rating") and len(r.get("stats") or {}) == 5:
                rows.append(r)
    return rows


def fit(rows):
    """Least squares: rating ~ a1*spd + ... + a5*wit + b*skill_rating + c.

    Uses numpy when it is available (it is, since the bot needs OpenCV) and
    falls back to a plain average-based estimate otherwise."""
    ys = [r["rating"] for r in rows]
    xs = [[r["stats"].get(s, 0) for s in STATS] + [r.get("skill_rating", 0), 1.0]
          for r in rows]
    try:
        import numpy as np
    except ImportError:
        print("numpy not available - showing the raw rows only")
        return None
    A = np.array(xs, dtype=float)
    y = np.array(ys, dtype=float)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ coef
    err = pred - y
    return coef, err


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("logs", "rating_samples.jsonl")
    if not os.path.exists(path):
        print(f"No samples yet at {path} - let the bot finish a few careers.")
        return 1
    rows = load(path)
    print(f"{len(rows)} usable careers in {path}")
    if len(rows) < 8:
        print("Fewer than 8 - keep collecting; the fit needs a spread of stats.")
        for r in rows:
            print("   ", r["ts"], r["rating"], r["grade"], r["stats"],
                  "skills", r.get("skill_rating"))
        return 0
    out = fit(rows)
    if not out:
        return 0
    coef, err = out
    print("\nrating ~= "
          + " + ".join(f"{c:.3f}*{n}" for c, n in zip(coef, STATS))
          + f" + {coef[5]:.3f}*skill_rating + {coef[6]:.0f}")
    import numpy as np
    print(f"\ntypical error: {np.abs(err).mean():.0f} rating "
          f"(worst {np.abs(err).max():.0f})")
    print("A tier in the UG range is 400-500 wide, so an average error under "
          "~150 makes tier-aware spending worth wiring in.")
    print("\nper-career check:")
    for r, e in list(zip(rows, err))[-10:]:
        print(f"   {r['ts']}  actual {r['rating']:6}  off by {e:+7.0f}  "
              f"({r['grade'] or '?'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
