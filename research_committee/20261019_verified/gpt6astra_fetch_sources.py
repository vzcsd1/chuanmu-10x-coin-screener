"""gpt6astra: bounded public-source evidence capture, no market/API scans."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
import json
from pathlib import Path
import re
import sys

import requests

ROOT = Path(__file__).resolve().parent
SOURCES = [
    ("GH02", "https://arxiv.org/abs/1003.0146"),
    ("GH03", "https://arxiv.org/abs/1506.02629"),
    ("GH04", "https://www.microsoft.com/en-us/research/publication/the-sybil-attack/"),
]


def fetch(item):
    source_id, url = item
    record = {"id": source_id, "url": url, "model_tag": "gpt6astra"}
    try:
        response = requests.get(url, timeout=(8, 20), headers={"User-Agent": "ResearchEvidence/1.0"})
        record.update(status=response.status_code, final_url=response.url,
                      retrieved_utc=datetime.now(timezone.utc).isoformat())
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", response.text, flags=re.I | re.S)
        body = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", body))).strip()
        if "arxiv" in url:
            start = body.find("Title:")
            record["text"] = body[max(0, start - 200): start + 6500]
        else:
            start = body.find("The Sybil Attack John")
            record["text"] = body[max(0, start):start + 2500]
    except requests.RequestException as error:
        record.update(status="failed", error=str(error))
    return record


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(fetch, SOURCES))
    target = ROOT / "raw_output" / "gpt6astra_public_capture.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"model_tag": "gpt6astra", "records": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in records:
        print(json.dumps({"id": row["id"], "status": row["status"], "text": row.get("text", "")[:1900]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
