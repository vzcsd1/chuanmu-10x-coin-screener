"""Compare observation thresholds on frozen historical features, not trade returns.

Writes a threshold comparison and every sample's old/new selection status.
Universe expansion and new spot-only coverage require separate forward observation.
"""
from pathlib import Path
import runpy
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import binance_box_strategy as base

DATA = ROOT / "data"
OLD_THRESHOLD = 6


def main() -> int:
    backtest = runpy.run_path(str(ROOT / "research/09_backtest_revised.py"), run_name="pool_review")
    samples = backtest["add_scores"](backtest["load"]())
    threshold = base.Config().min_score
    positive = samples["label"] == 1
    tenfold = positive & (samples["gain"] >= 9)
    rows = []
    for value in sorted({3, 4, 5, OLD_THRESHOLD, threshold}):
        selected = samples["score_full"] >= value
        hits = int((selected & positive).sum())
        count = int(selected.sum())
        rows.append({
            "min_score": value, "selected": count, "positive": hits,
            "control": int((selected & ~positive).sum()),
            "precision": hits / count if count else 0,
            "recall": hits / int(positive.sum()),
            "tenfold_selected": int((selected & tenfold).sum()),
            "tenfold_total": int(tenfold.sum()),
        })
    comparison = pd.DataFrame(rows)
    comparison.to_csv(DATA / "candidate_pool_comparison.csv", index=False, encoding="utf-8-sig")
    details = samples[["symbol", "pre_day", "label", "gain", "score_kline",
                       "score_deriv", "score_full", "has_deriv"]].copy()
    details["old_min_score"] = OLD_THRESHOLD
    details["new_min_score"] = threshold
    details["old_selected"] = details["score_full"] >= OLD_THRESHOLD
    details["new_selected"] = details["score_full"] >= threshold
    details["newly_included"] = details["new_selected"] & ~details["old_selected"]
    details.to_csv(DATA / "candidate_pool_samples.csv", index=False, encoding="utf-8-sig")
    print(comparison.to_string(index=False))
    print(f"Saved {len(details)} sample rows to data/candidate_pool_samples.csv")
    print("Historical case comparison only; not live win rate or a universe-expansion backtest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
