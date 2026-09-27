#!/usr/bin/env python3
"""Compare private, locally generated Dr.CGM reports with operator reviewed cases."""
import argparse
import json
from collections import Counter
from pathlib import Path

from cgmes_diag.common import write_csv, write_json


def evaluate(labels, base):
    if not isinstance(labels, dict) or set(labels) != {"cases"} or not isinstance(labels["cases"], list):
        raise ValueError("Labels must contain exactly a cases list")
    seen, rows = set(), []
    for case in labels["cases"]:
        if not isinstance(case, dict) or set(case) != {"id", "report", "expected"}:
            raise ValueError("Each case requires id, report and expected only")
        case_id, expected = case["id"], case["expected"]
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("Case IDs must be distinct nonempty strings")
        seen.add(case_id)
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"{case_id}: expected must be a nonempty list")
        report_path = Path(case["report"])
        if report_path.is_absolute():
            raise ValueError(f"{case_id}: report path must be relative to the labels file")
        report = json.loads((base / report_path / "report.json").read_text(encoding="utf-8"))
        counts = {(x["scope"], x["code"]): x["count"] for x in report["finding_counts"]}
        engines = {x["scope"]: x for x in report["engines"]}
        for target in expected:
            if not isinstance(target, dict) or set(target) != {"scope", "code"}:
                raise ValueError(f"{case_id}: expected entries require scope and code")
            scope, code = target["scope"], target["code"]
            if not isinstance(scope, str) or not isinstance(code, str) or not scope or not code:
                raise ValueError(f"{case_id}: scope and code must be nonempty strings")
            # A missing finding during incomplete replay is unknown, never a successful negative result.
            complete = report["status"] == "completed" and (
                scope in {"RUN", "ALIGNMENT"} or
                (scope in engines and engines[scope]["status"] == "completed")
            )
            count = counts.get((scope, code), 0)
            rows.append(dict(case_id=case_id, scope=scope, expected_code=code,
                             result="detected" if count else "missed" if complete else "inconclusive",
                             observed_count=count, report_status=report["status"]))
    summary = Counter(row["result"] for row in rows)
    return {"summary": {k: summary[k] for k in ("detected", "missed", "inconclusive")},
            "cases": len(seen), "checks": rows,
            "interpretation": "Only operator-labelled expected findings are scored. These counts are not diagnostic precision, recall or root-cause accuracy."}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--labels", type=Path, required=True, help="Private JSON labels file")
    p.add_argument("--out", type=Path, required=True, help="New private output directory")
    args = p.parse_args(argv)
    try:
        labels = json.loads(args.labels.read_text(encoding="utf-8-sig"))
        result = evaluate(labels, args.labels.resolve().parent)
        args.out.mkdir(parents=True, exist_ok=False)
        write_json(args.out / "benchmark.json", result)
        write_csv(args.out / "benchmark.csv", result["checks"],
                  ["case_id", "scope", "expected_code", "result", "observed_count", "report_status"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        p.error(str(exc))
    print(f"{result['cases']} cases: {result['summary']}. Results saved in {args.out}")
    return 1 if result["summary"]["missed"] else 2 if result["summary"]["inconclusive"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
