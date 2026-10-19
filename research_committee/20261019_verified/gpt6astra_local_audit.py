"""gpt6astra: read-only checks of current code and saved historical rows."""
import ast
import csv
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def read_csv(relative):
    with (ROOT / relative).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    code = ROOT / "binance_box_strategy.py"
    tree = ast.parse(code.read_text(encoding="utf-8-sig"))
    config = {}
    weights = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "WEIGHTS":
            weights = ast.literal_eval(node.value)
        if isinstance(node, ast.ClassDef) and node.name == "Config":
            for field in node.body:
                if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                    config[field.target.id] = ast.literal_eval(field.value)
    old = read_csv("data/hit_rate_current.csv")
    positive = [row for row in old if float(row["label"]) == 1]
    ten = [row for row in positive if float(row["gain"]) >= 9]
    result = {
        "model_tag": "gpt6astra", "research_date": "2026-10-19",
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "scope": "offline code defaults and saved rows, not runtime configuration or trading validation",
        "config_defaults": config, "weights": weights,
        "saved_event_sample": {"rows": len(old), "positive": len(positive),
                               "controls": len(old) - len(positive),
                               "positive_hit": sum(row["hit"].lower() == "true" for row in positive),
                               "tenfold_events": len(ten),
                               "tenfold_hits": sum(row["hit"].lower() == "true" for row in ten),
                               "positive_without_deriv": sum(row["has_deriv"].lower() != "true" for row in positive)},
        "protected_files": [],
        "limits": ["Case-enriched event sample is not market incidence or a trading win rate.",
                   "A1 r3 peer reviews are read as evidence, not newly rerun or finally accepted here.",
                   "No live service, market download, parameter change or account action is executed."],
    }
    paths = ["binance_box_strategy.py", "ui/app.py", "research/02_detect_pumps.py", "research/03_build_features.py",
             "research/05_deriv_features.py", "research/07_oos_validate.py", "research/08_audit_hindsight.py",
             "research/09_backtest_revised.py", "research/11_hit_rate_current.py", "research/12_optimize_gate.py",
             "data/hit_rate_current.csv", "data/backtest_scores.csv"]
    for name in paths:
        body = (ROOT / name).read_bytes()
        result["protected_files"].append({"path": name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
    output = HERE / "raw_output" / "gpt6astra_local_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"model_tag": "gpt6astra", "min_score": config["min_score"], "max_symbols": config["max_symbols"],
                      "sample": result["saved_event_sample"], "protected_files": len(paths)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
