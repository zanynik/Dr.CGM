import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from . import __version__
from .common import Findings, write_json
from .report import render
from .source import SourceIndex, header_kinds


def validate_manifest(data):
    allowed = {"igms", "cgm", "boundaries", "common_data", "required_profiles", "engine", "max_input_bytes", "max_examples_per_rule", "localization", "alignment_observations"}
    if set(data) - allowed: raise ValueError(f"Unknown manifest fields: {sorted(set(data)-allowed)}")
    if bool(data.get("igms")) == bool(data.get("cgm")):raise ValueError("Supply exactly one of nonempty igms or cgm; do not mix original and RCC-updated state profiles")
    if "igms" in data and not isinstance(data["igms"],list): raise ValueError("igms must be a list")
    if data.get("cgm") and (not isinstance(data["cgm"],dict) or set(data["cgm"])!={"files"} or not isinstance(data["cgm"]["files"],list) or not data["cgm"]["files"] or not all(isinstance(x,str) for x in data["cgm"]["files"])):raise ValueError("cgm requires a nonempty files list of paths")
    names = set()
    for g in data.get("igms",[]):
        name = g.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) or name.upper() in {"CGM", "BOUNDARY", "COMMON", "CGM_PACKAGE"} or name.lower() in names or name.upper().startswith("SUBSET_"):
            raise ValueError("IGM names must be unique safe labels, excluding CGM and BOUNDARY")
        names.add(name.lower())
        if not isinstance(g.get("files"), list) or not g["files"] or not all(isinstance(x,str) for x in g["files"]): raise ValueError(f"{name}: files must be a nonempty list of paths")
    if not isinstance(data.get("boundaries", []), list) or not all(isinstance(x,str) for x in data.get("boundaries", [])):
        raise ValueError("boundaries must be a list of paths")
    if not isinstance(data.get("common_data",[]),list) or not all(isinstance(x,str) for x in data.get("common_data",[])):raise ValueError("common_data must be a list of paths")
    engine = data.get("engine", {})
    valid_engine = {"timeout_seconds", "experiments", "experiments_on_success", "import_parameters", "loadflow_parameters",
                    "voltage_target_min_pu", "voltage_target_max_pu", "voltage_target_spread_kv", "near_zero_impedance_pu",
                    "boundary_p_tolerance_mw", "expected_internal_boundary_keys", "max_examples_per_rule", "validation_threshold",
                    "control_experiments", "area_net_position_targets_mw", "net_position_tolerance_mw", "rated_voltage_min_pu", "rated_voltage_max_pu",
                    "solved_voltage_min_pu", "solved_voltage_max_pu", "reactive_margin_mvar"}
    if set(engine)-valid_engine: raise ValueError(f"Unknown engine fields: {sorted(set(engine)-valid_engine)}")
    timeout=float(engine.get("timeout_seconds",300))
    if not math.isfinite(timeout) or timeout <= 0: raise ValueError("timeout_seconds must be finite and positive")
    if int(data.get("max_examples_per_rule",200))<1: raise ValueError("max_examples_per_rule must be positive")
    for key in ("experiments","experiments_on_success"):
        if key in engine and not isinstance(engine[key],bool): raise ValueError(f"{key} must be a JSON boolean")
    imp = engine.get("import_parameters", {})
    if not all(isinstance(v,str) for v in imp.values()): raise ValueError("import_parameters values must be strings")
    if imp.get("iidm.import.cgmes.cgm-with-subnetworks-defined-by") == "FILENAME":
        raise ValueError("FILENAME subnetwork grouping unsupported by the staged replay; use MODELING_AUTHORITY")
    controls=engine.get("control_experiments",[])
    if not isinstance(controls,list) or len(controls)>10:raise ValueError("control_experiments must be a list of at most ten explicitly selected controls")
    for c in controls:
        if set(c)!={"kind","element_id"} or c["kind"] not in {"generator","ratio_tap","phase_tap","shunt"} or not isinstance(c["element_id"],str):raise ValueError("Invalid control experiment")
    loc=data.get("localization",{})
    if set(loc)-{"enabled","max_runs","subsets"}:raise ValueError("Unknown localization option")
    if not isinstance(loc.get("enabled",False),bool):raise ValueError("localization.enabled must be boolean")
    if not 1<=int(loc.get("max_runs",4))<=20:raise ValueError("localization.max_runs must be 1..20")
    if loc.get("enabled") and not data.get("igms"):raise ValueError("Localization requires per-IGM packages")
    exact_names={g["name"] for g in data.get("igms",[])}
    for subset in loc.get("subsets",[]):
        if not isinstance(subset,list) or not subset or len(set(subset))!=len(subset) or set(subset)-exact_names:raise ValueError("Each localization subset must name distinct supplied IGMs")
    for row in data.get("alignment_observations",[]):
        if set(row)-{"area","original_mw","target_mw","aligned_mw","tolerance_mw","source"} or not {"area","original_mw","target_mw","aligned_mw","source"}<=set(row):raise ValueError("Alignment observations require area, original_mw, target_mw, aligned_mw and source")
        if not all(isinstance(row[k],(int,float)) and not isinstance(row[k],bool) and math.isfinite(row[k]) for k in ("original_mw","target_mw","aligned_mw")):raise ValueError("Alignment values must be finite numbers")
        if not isinstance(row.get("tolerance_mw",5.),(int,float)) or not math.isfinite(row.get("tolerance_mw",5.)) or row.get("tolerance_mw",5.)<0:raise ValueError("Alignment tolerance must be finite and nonnegative")
    targets=engine.get("area_net_position_targets_mw",{})
    if not isinstance(targets,dict) or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in targets.values()):raise ValueError("Area targets must map Area IDs to finite MW numbers")
    if int(data.get("max_input_bytes", 4*1024**3)) <= 0: raise ValueError("max_input_bytes must be positive")
    return data


def assemble(rows, target):
    seen = set()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as z:
        for row in rows:
            if row["sha256"] in seen: continue
            seen.add(row["sha256"])
            z.write(row["staged"], f"profile_{row['id']:06d}.xml")


def run_engine(scope, rows, workspace, out, engine_config):
    if not rows or any(not r["parsed"] for r in rows):
        return dict(scope=scope, status="skipped", reason="No input or unreadable source XML. Engine replay would be incomplete.")
    if any(h["kind"] == "DifferenceModel" for r in rows for h in r["headers"]):
        return dict(scope=scope, status="skipped", reason="DifferenceModels require baseline materialization.")
    archive = workspace / f"{scope}.zip"
    assemble(rows, archive)
    folder = out / "engine" / scope
    folder.mkdir(parents=True)
    job = dict(scope=scope, input=str(archive), out=str(folder), config=engine_config,
               has_sv=any("SV" in header_kinds(header) for row in rows for header in row["headers"]))
    job_path = workspace / f"{scope}-job.json"
    write_json(job_path, job)
    expired = False
    with (folder / "worker-console.txt").open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run([sys.executable, "-m", "cgmes_diag.engine", str(job_path)],
                                  cwd=Path(__file__).resolve().parent.parent, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=float(engine_config.get("timeout_seconds", 300)), check=False)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            expired, code = True, -1
    result_file = folder / "result.json"
    result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else dict(scope=scope, status="failed", error="Worker did not produce a result; see worker-console.txt")
    if expired:
        result.update(status="timed_out", error="Per-scope time budget exceeded; only checkpointed stages are available.")
    elif code != 0 and result.get("status") == "completed":
        result.update(status="failed", error=f"Worker exited with code {code}")
    result.update(directory=f"engine/{scope}", exit_code=code)
    write_json(result_file, result)
    archive.unlink(missing_ok=True)
    return result


def localize(manifest, groups, workspace, out, config, baseline, findings):
    settings=manifest.get("localization",{})
    if not settings.get("enabled",False):return {"status":"not_requested","runs":[]}
    if baseline.get("status")!="completed" or baseline.get("baseline_converged"):
        return {"status":"skipped","reason":"Requires a completed, non-converging full-CGM AC baseline.","runs":[]}
    names=[g["name"] for g in manifest.get("igms",[])]
    requested=settings.get("subsets") or [[other for other in names if other!=excluded] for excluded in names]
    requested=[s for s in requested if s and set(s)!=set(names)]
    shared=groups.get("BOUNDARY",[])+groups.get("COMMON",[])
    runs=[]
    for number,subset in enumerate(requested[:int(settings.get("max_runs",4))],1):
        scope=f"SUBSET_{number:03d}"
        print(f"Testing subset {subset}...",flush=True)
        rows=shared+[row for name in subset for row in groups[name]]
        # Solver sensitivity experiments are unnecessary inside the region search.
        result=run_engine(scope,rows,workspace,out,{**config,"experiments":False,"control_experiments":[]})
        result.update(included=subset,excluded=sorted(set(names)-set(subset)))
        runs.append(result)
        if result.get("status")=="completed" and result.get("baseline_converged"):
            findings.add("REGION_OMISSION_RESTORED_CONVERGENCE","WARN","CGM","A reduced IGM subset converged while the full CGM failed.","Inspect omitted areas and their boundary/control interactions. Removing an IGM changes the physical problem; this is not proof of a faulty area or a monotonic binary-search result.",{"included":subset,"excluded":result["excluded"],"directory":result["directory"]},certainty="hypothesis")
    return {"status":"completed" if all(r["status"]=="completed" for r in runs) else "incomplete",
            "runs":runs,"requested":len(requested),"budget":settings.get("max_runs",4),"untested":max(0,len(requested)-len(runs)),
            "interpretation":"Bounded subset replay; failed imports/timeouts are inconclusive, and missing-neighbor boundary equivalents can change feasibility."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline CGMES source triage plus per-IGM/combined-CGM PyPowSyBl replay.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="New output directory (must not already exist)")
    parser.add_argument("--preflight-only", action="store_true", help="Inspect source files without importing PyPowSyBl")
    parser.add_argument("--previous", type=Path, help="Previous report directory; compare CIM statements against source-index.sqlite")
    args = parser.parse_args(argv)
    try:
        manifest = validate_manifest(json.loads(args.manifest.read_text(encoding="utf-8-sig")))
        out = args.out.resolve()
        out.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        parser.error(str(exc))
    now = datetime.now(timezone.utc)
    f = Findings(manifest.get("max_examples_per_rule", 200))
    data = dict(tool_version=__version__, run_id=now.strftime("%Y%m%dT%H%M%SZ"), created_utc=now.isoformat(),
                mode="source preflight only" if args.preflight_only else "source + upstream replay", status="running", engines=[], findings=[], finding_counts=[])
    write_json(out / "manifest-resolved.json", dict(manifest=manifest, manifest_path=str(args.manifest.resolve()), input_base=str(args.manifest.resolve().parent)))
    index = None
    try:
        index = SourceIndex(out / "source-index.sqlite", f)
        with tempfile.TemporaryDirectory(prefix="cgmes-diagnose-") as tmp:
            workspace = Path(tmp)
            print("Indexing source CGMES profiles...", flush=True)
            groups = index.stage(manifest, args.manifest.resolve().parent, workspace, int(manifest.get("max_input_bytes", 4*1024**3)))
            index.check(manifest)
            write_json(out / "inventory.json", [{k:v for k,v in row.items() if k != "staged"} for row in index.files])
            if args.previous:
                data["comparison"] = index.compare(args.previous / "source-index.sqlite", out / "changes.csv")
                data["comparison_previous_report"]=str(args.previous.resolve())
                old=json.loads((args.previous/"manifest-resolved.json").read_text(encoding="utf-8"))["manifest"]
                data["configuration_comparison"]={key:{"previous":old.get(key),"current":manifest.get(key)} for key in ("engine","required_profiles","localization","alignment_observations") if old.get(key)!=manifest.get(key)}
            data["alignment_observations"]=[]
            for row in manifest.get("alignment_observations",[]):
                item={**row,"required_change_mw":row["target_mw"]-row["original_mw"],"applied_change_mw":row["aligned_mw"]-row["original_mw"],"residual_mw":row["aligned_mw"]-row["target_mw"],"provenance":"User-supplied measurements, not calculated by Dr.CGM."}
                data["alignment_observations"].append(item)
                if abs(item["residual_mw"])>row.get("tolerance_mw",5.):f.add("ALIGNMENT_TARGET_MISSED","WARN","ALIGNMENT","Reported post-alignment net position misses its target.","Verify production scaling inputs, GLSK/participation and recorded stage; Dr.CGM did not apply these changes.",item,certainty="observed")
            if not args.preflight_only:
                boundary = groups.get("BOUNDARY", [])+groups.get("COMMON",[])
                scopes = [(g["name"], groups.get(g["name"], []) + boundary) for g in manifest.get("igms",[])]
                # One combined CGMES import gives the converter all IGM/boundary references at once;
                # it is deliberately not a blind Network.merge of independently converted IIDM objects.
                scopes.append(("CGM", [r for rows in groups.values() for r in rows]))
                engine_config = {"max_examples_per_rule":manifest.get("max_examples_per_rule",200), **manifest.get("engine", {})}
                for scope, rows in scopes:
                    print(f"Replaying {scope} ({len(rows)} profiles)...", flush=True)
                    result = run_engine(scope, rows, workspace, out, engine_config)
                    data["engines"].append(result)
                    if result["status"] != "completed":
                        f.add("REPLAY_INCOMPLETE", "ERROR", scope, result.get("error", result.get("reason", "Incomplete worker")), "Inspect the recorded coverage and worker logs; no successful CGM diagnosis is implied.")
                data["localization"]=localize(manifest,groups,workspace,out,engine_config,data["engines"][-1],f)
            else:
                data["engines"] = [dict(scope="ALL", status="not_requested", reason="Preflight-only: no electrical replay performed.")]
            for finding in f.items:
                finding["source_trace"] = []
                for sid in finding.get("ids",[]):
                    matches = index.trace(sid)
                    if matches: finding["source_trace"].append(dict(source_id=sid,matches=matches))
            for result in data["engines"]:
                aliases = defaultdict_aliases(result.get("network", {}).get("aliases", []))
                for finding in result.get("findings", []):
                    traces = []
                    for sid in finding.get("ids", []):
                        candidates = [sid] + aliases.get(sid, [])
                        for candidate in candidates:
                            matches = index.trace(candidate)
                            if matches: traces.append(dict(engine_id=sid, matched_id=candidate, matches=matches))
                    finding["source_trace"] = traces
                    if finding.get("ids") and not traces:
                        finding["source_trace_note"] = "No exact RDF/mRID/alias match; converted bus/equipment IDs are not guessed. Inspect conversion report."
            data["findings"] = f.items + [x for r in data["engines"] for x in r.get("findings", [])]
            data["finding_counts"] = f.summary() + [x for r in data["engines"] for x in r.get("finding_counts", [])]
            statuses = [r["status"] for r in data["engines"]]
            data["status"] = "incomplete" if any(s not in ("completed", "not_requested") for s in statuses) or any(not r["parsed"] for r in index.files) else "completed"
            if data.get("localization",{}).get("status")=="incomplete":data["status"]="incomplete"
            igms = [r for r in data["engines"] if r["scope"] != "CGM"]
            cgm = next((r for r in data["engines"] if r["scope"] == "CGM"), {})
            if igms and all(r.get("baseline_converged") for r in igms) and cgm.get("status") == "completed" and not cgm.get("baseline_converged"):
                data["assembly_interpretation"] = "All submitted IGMs converged individually, but combined replay failed. This narrows investigation to assembly interactions, boundary pairing, controls and balance; it does not establish a single root cause."
            else:
                data["assembly_interpretation"] = "Compare individual IGM and combined results with source findings. An IGM may depend on boundary equivalents and can behave differently when assembled."
    except Exception as exc:
        f.add("DIAGNOSTIC_INCOMPLETE", "ERROR", "RUN", f"{type(exc).__name__}: {exc}", "Correct the input/configuration or inspect the incomplete diagnostic stage and rerun to a new output directory.")
        data.update(status="incomplete", findings=f.items + [x for r in data["engines"] for x in r.get("findings",[])],
                    finding_counts=f.summary() + [x for r in data["engines"] for x in r.get("finding_counts",[])])
        if index: write_json(out / "inventory.json", [{k:v for k,v in row.items() if k != "staged"} for row in index.files])
    finally:
        if index: index.close()
    render(out, data)
    errors = sum(x["count"] for x in data["finding_counts"] if x["severity"] == "ERROR")
    print(f"{data['status']}: {errors} error findings. Open {out / 'report.html'}", flush=True)
    return 2 if data["status"] == "incomplete" else (1 if errors else 0)


def defaultdict_aliases(rows):
    result = {}
    for row in rows: result.setdefault(str(row.get("id", "")), []).append(str(row.get("alias", "")))
    return result
