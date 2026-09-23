#!/usr/bin/env python3
"""Build an explicit Dr.CGM manifest from a local ReliCapGrid checkout; no data copied."""
import argparse
import json
import subprocess
from pathlib import Path

NON_TSO = {"boundaryData", "commonData", "referenceData", "NetworkCode", "Jotunheim"}
TESTED_COMMIT = "f09bb340795122b43d6499e913c121bb21458859"


def required(folder, pattern):
    files = sorted(folder.glob(pattern))
    if not files:
        raise ValueError(f"Missing expected input: {folder / pattern}")
    return [str(f.resolve()) for f in files]


def build(repo, mode="cgm", tsos=None):
    instance = repo.resolve() / "Instance"
    available = sorted(p.name for p in instance.iterdir() if p.is_dir() and p.name not in NON_TSO and (p / "Grid/cimxml").is_dir())
    selected = tsos or available
    if not selected or set(selected) - set(available):
        raise ValueError(f"Choose existing TSO names: {available}")
    if mode == "cgm" and set(selected) != set(available):
        raise ValueError("RCC TP/SV describe the full CGM. Partial TSO selection is supported only in igm mode.")
    boundary = required(instance / "boundaryData/Grid/cimxml", "*.xml")
    common = required(instance / "commonData/Grid/cimxml", "Grid_CommonData_CGM-CD.xml")
    result = {"boundaries": boundary, "common_data": common,
              "engine": {"timeout_seconds": 600, "experiments": True,
                         "loadflow_parameters": {"distributed_slack": False,
                             "voltage_init_mode": "UNIFORM_VALUES",
                             "transformer_voltage_control_on": True,
                             "phase_shifter_regulation_on": True,
                             "shunt_compensator_voltage_control_on": True,
                             "use_reactive_limits": True,
                             "provider_parameters": {"maxNewtonRaphsonIterations": "25", "maxOuterLoopIterations": "20"}}}}
    if mode == "cgm":
        # RCC topology spans modeling authorities. With this pinned reference package,
        # native per-authority subnetworks fail to resolve voltage levels; record the
        # explicit alternative instead of silently retrying or changing input data.
        result["engine"]["import_parameters"] = {"iidm.import.cgmes.cgm-with-subnetworks": "false"}
        files = [f for tso in selected for f in required(instance / tso / "Grid/cimxml", "*_EQ_*.xml")]
        for profile in ("TP", "SV"):
            files += required(instance / "Jotunheim/Grid/cimxml", f"*_{profile}_*.xml")
        # These are complete updated SSH profiles, not unmaterialized DifferenceModels.
        for tso in selected:
            delivery_name = "HVDC_" + tso[3:] if tso.startswith("DC-") else tso
            files += required(instance / "Jotunheim/ChangeSet/cimxml", f"*_{delivery_name}_SSH_*.xml")
        result["cgm"] = {"files": files}
    else:
        result["igms"] = [{"name": tso, "files": required(instance / tso / "Grid/cimxml", "*.xml")} for tso in selected]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--mode", choices=("cgm", "igm"), default="cgm")
    parser.add_argument("--tsos", nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.out.exists(): raise ValueError("Output manifest already exists; choose a new path")
        value = build(args.repo, args.mode, args.tsos)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        try:
            commit = subprocess.check_output(["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True).strip()
        except (OSError, subprocess.CalledProcessError): commit = "unknown (Git metadata unavailable)"
        provenance = {"repository": "https://github.com/entsoe/relicapgrid", "commit": commit,
                      "packaging_reference_commit": TESTED_COMMIT, "mode": args.mode,
                      "notes": "No models copied or rewritten. Solver preset is a partial mapping, not certified reproduction of reference settings. All boundary files included for dependency closure; inspect unused/external boundaries."}
        args.out.with_suffix(".provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Created {args.out}; ReliCapGrid commit: {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
