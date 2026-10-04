from pathlib import Path
import runpy


root = Path(__file__).resolve().parent
targets = [p for p in root.glob("*.py") if "十倍币筛选" in p.name]
if not targets:
    raise FileNotFoundError("川沐筛选脚本未找到")
runpy.run_path(str(targets[0]), run_name="__main__")
