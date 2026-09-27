import json

from benchmark import evaluate
from cgmes_diag.report import render


def test_benchmark_uses_full_counters_and_does_not_count_incomplete_as_miss(tmp_path):
    complete = tmp_path / "complete"
    complete.mkdir()
    (complete / "report.json").write_text(json.dumps({
        "status": "completed", "finding_counts": [{"scope": "CGM", "code": "AC_NOT_CONVERGED", "count": 9}],
        "findings": [], "engines": [{"scope": "CGM", "status": "completed"}],
    }))
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "report.json").write_text(json.dumps({
        "status": "incomplete", "finding_counts": [],
        "findings": [], "engines": [{"scope": "CGM", "status": "timed_out"}],
    }))
    labels = {"cases": [
        {"id": "known-failure", "report": "complete", "expected": [
            {"scope": "CGM", "code": "AC_NOT_CONVERGED"},
            {"scope": "CGM", "code": "EXPECTED_BOUNDARY_UNPAIRED"}]},
        {"id": "timed-out", "report": "incomplete", "expected": [
            {"scope": "CGM", "code": "AC_NOT_CONVERGED"}]},
    ]}
    result = evaluate(labels, tmp_path)
    assert result["summary"] == {"detected": 1, "missed": 1, "inconclusive": 1}
    assert result["checks"][0]["observed_count"] == 9


def test_report_includes_method_and_limit_for_known_rules(tmp_path):
    data = {"run_id": "test", "created_utc": "test", "status": "completed", "mode": "test", "tool_version": "test",
            "finding_counts": [{"severity": "ERROR", "count": 1}], "engines": [],
            "findings": [{"code": "AC_NOT_CONVERGED", "severity": "ERROR", "scope": "CGM",
                          "certainty": "observed", "message": "failed", "action": "check", "ids": [], "evidence": {}}]}
    render(tmp_path, data)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert "baseline AC" in saved["findings"][0]["methodology"]["method"]
    assert "does not identify its cause" in (tmp_path / "report.html").read_text()
