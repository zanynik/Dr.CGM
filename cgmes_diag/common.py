import csv
import json
import math
from collections import Counter
from pathlib import Path


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(clean(data), indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


class Findings:
    """Full counts, bounded examples. No invented probabilistic confidence."""
    def __init__(self, limit=200):
        self.items = []
        self.counts = Counter()
        self.limit = limit

    def add(self, code, severity, scope, message, action, evidence=None, ids=None, certainty="observed"):
        key = (code, severity, scope)
        self.counts[key] += 1
        if self.counts[key] <= self.limit:
            self.items.append(dict(code=code, severity=severity, scope=scope, message=message,
                                   action=action, evidence=clean(evidence or {}), ids=ids or [], certainty=certainty))

    def summary(self):
        return [dict(code=k[0], severity=k[1], scope=k[2], count=v,
                     examples_saved=min(v, self.limit)) for k, v in sorted(self.counts.items())]


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for key in fields:
                val = row.get(key, "")
                if isinstance(val, (dict, list, tuple)):
                    val = json.dumps(clean(val), ensure_ascii=False)
                # Prevent CSV formula execution when opening externally supplied names in Excel.
                if isinstance(val, str) and val.startswith(("=", "+", "-", "@", "\t", "\r")):
                    val = "'" + val
                out[key] = val
            writer.writerow(out)
