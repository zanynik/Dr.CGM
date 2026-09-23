"""PyPowSyBl adapter. Each scope runs in a disposable process with a time limit."""
import inspect
import contextlib
import io
import json
import logging
import math
import platform
import re
import sys
import time
from pathlib import Path
from .common import Findings, clean, write_csv, write_json
from .experiments import plan as experiment_plan, merge_parameters, disable_control, report_events
from .advanced import inspect_extended, area_positions, inspect_solution


def finite(x):
    try: return math.isfinite(float(x))
    except (ValueError, TypeError): return False


def record_frame(frame):
    return clean(frame.reset_index().to_dict("records"))


def save_frame(path, frame):
    rows = frame.reset_index()
    write_csv(path, rows.to_dict("records"), list(rows.columns))


def save_report(out, name, report):
    (out / f"{name}.txt").write_text(str(report), encoding="utf-8")
    try:
        value = report.to_json()
        (out / f"{name}.json").write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    except Exception as exc:
        (out / f"{name}-json-error.txt").write_text(str(exc), encoding="utf-8")


def import_report_findings(report, f, scope):
    """Keep upstream wording/values as evidence; do not infer a root cause from a log keyword."""
    try:
        data = json.loads(report.to_json())
        stack = [data.get("reportRoot", {})]
        while stack:
            node = stack.pop()
            stack.extend(node.get("children", []))
            values = {k: v.get("value") if isinstance(v, dict) else v for k,v in node.get("values", {}).items()}
            severity = str(values.get("reportSeverity", "")).upper()
            if severity in ("WARN", "WARNING", "ERROR"):
                key = node.get("messageKey", "")
                ids = [str(v) for k,v in values.items() if k.lower().endswith("id") and v is not None]
                f.add("IMPORT_REPORTED_ISSUE", "ERROR" if severity == "ERROR" else "WARN", scope,
                      f"CGMES importer reported {key}.", "Read import-report.txt for the engine's full message and compare original source values with the converted network.",
                      {"message_key":key,"values":values}, ids)
    except Exception as exc:
        f.add("IMPORT_REPORT_PARSE_UNAVAILABLE", "INFO", scope, "Structured import-report extraction was unavailable.", "Use the complete saved text report.", {"error":str(exc)})


def final_mismatches(report, n, failed_components):
    """Expose final reported NR residual locations and exact connected IIDM equipment."""
    data = json.loads(report.to_json())
    latest = {}
    def walk(node, context):
        values = {k:v.get("value") if isinstance(v,dict) else v for k,v in node.get("values",{}).items()}
        ctx = {**context, **{k:v for k,v in values.items() if k in ("networkNumCc","networkNumSc","iteration")}}
        if node.get("messageKey") == "olf.NRMismatch":
            key = (ctx.get("networkNumCc"),ctx.get("networkNumSc"),values.get("equationType"))
            merged = {**ctx,**values}
            for child in node.get("children",[]):
                merged.update({k:v.get("value") if isinstance(v,dict) else v for k,v in child.get("values",{}).items()})
            latest[key] = merged
        for child in node.get("children",[]): walk(child, ctx)
    walk(data.get("reportRoot",{}), {})
    terminals = n.get_terminals()
    result = []
    for (cc, sc, _), row in latest.items():
        if (cc,sc) not in failed_components or not finite(row.get("mismatch")) or abs(row["mismatch"])<1e-7: continue
        bus = row.get("busId")
        neighbors = terminals[terminals.bus_id==bus] if bus else terminals.iloc[0:0]
        row["connected_equipment"] = list(dict.fromkeys(map(str,neighbors.index)))[:30]
        row["connected_equipment_count"] = len(set(neighbors.index))
        result.append(row)
    return result


def inspect_network(n, scope, out, f, config):
    import pandas as pd
    tables, coverage = {}, []
    methods = {
        "buses": "get_buses", "voltage_levels": "get_voltage_levels", "generators": "get_generators",
        "loads": "get_loads", "batteries": "get_batteries", "lines": "get_lines",
        "transformers2": "get_2_windings_transformers", "transformers3": "get_3_windings_transformers",
        "ratio_taps": "get_ratio_tap_changers", "phase_taps": "get_phase_tap_changers",
        "shunts": "get_shunt_compensators", "svcs": "get_static_var_compensators",
        "vsc": "get_vsc_converter_stations", "lcc": "get_lcc_converter_stations",
        "hvdc": "get_hvdc_lines", "ties": "get_tie_lines", "aliases": "get_aliases",
        "switches": "get_switches", "substations": "get_substations", "areas": "get_areas",
        "area_boundaries": "get_areas_boundaries", "area_voltage_levels": "get_areas_voltage_levels",
        "boundary": "get_boundary_lines" if hasattr(n, "get_boundary_lines") else "get_dangling_lines",
        "limits": "get_loading_limits", "terminals": "get_terminals",
        "boundary_generation": "get_boundary_lines_generation" if hasattr(n, "get_boundary_lines_generation") else "get_dangling_lines_generation",
    }
    for label, method in methods.items():
        try:
            fn = getattr(n, method)
            kw = {"all_attributes": True} if "all_attributes" in inspect.signature(fn).parameters else {}
            if label=="limits": kw["show_inactive_sets"]=True
            tables[label] = fn(**kw)
            save_frame(out / f"network-{label}.csv", tables[label])
            coverage.append(dict(check=label, status="completed", rows=len(tables[label])))
        except Exception as exc:
            tables[label] = pd.DataFrame()
            coverage.append(dict(check=label, status="failed", error=str(exc)))
            f.add("ENGINE_CHECK_FAILED", "WARN", scope, f"Could not inspect {label}: {exc}", "Inspect engine/API compatibility; this check is incomplete.")
    buses, gens, loads, volts, boundary = [tables[x] for x in ("buses", "generators", "loads", "voltage_levels", "boundary")]
    def issue(code, message, action, sid, row, certainty="observed", severity="WARN"):
        f.add(code, severity, scope, message, action, dict(row), [str(sid)], certainty)
    for sid, row in gens.iterrows():
        if not row.get("connected", False): continue
        p, lo, hi = row.get("target_p"), row.get("min_p"), row.get("max_p")
        if all(finite(v) for v in (p, lo, hi)) and (lo > hi or p < lo - 1e-6 or p > hi + 1e-6):
            issue("GENERATOR_P_LIMIT", "Generator scheduled P is outside active power limits.", "Check SSH setpoint, sign convention and EQ GeneratingUnit limits; verify balancing eligibility.", sid, row)
        qlo, qhi = row.get("min_q_at_target_p"), row.get("max_q_at_target_p")
        if all(finite(v) for v in (qlo, qhi)) and qlo > qhi:
            issue("GENERATOR_Q_LIMIT_ORDER", "Generator reactive limits are inverted at target P.", "Check reactive capability curve points and units.", sid, row, severity="ERROR")
        if row.get("voltage_regulator_on", False):
            bid = row.get("regulated_bus_id") or row.get("bus_id")
            vlid = buses.loc[bid, "voltage_level_id"] if bid in buses.index else None
            nominal = volts.loc[vlid, "nominal_v"] if vlid in volts.index else None
            target = row.get("target_v")
            if finite(nominal) and nominal > 0 and finite(target):
                ratio = target / nominal
                if not config.get("voltage_target_min_pu", 0.8) <= ratio <= config.get("voltage_target_max_pu", 1.2):
                    issue("VOLTAGE_TARGET_IMPLAUSIBLE", f"Voltage target is {ratio:.3f} pu at its regulated bus.", "Check regulated terminal, nominal voltage and kV/pu conversion.", sid, {**row.to_dict(), "target_pu": ratio}, "hypothesis")
    # Conflicting controls are hypotheses: remote-control sharing/participation may be intentional.
    if not gens.empty:
        active = gens[gens.connected & gens.voltage_regulator_on]
        for bid, group in active.groupby("regulated_bus_id"):
            if bid and len(group) > 1 and group.target_v.max() - group.target_v.min() > config.get("voltage_target_spread_kv", 0.5):
                f.add("VOLTAGE_CONTROL_CONFLICT", "WARN", scope, "Generators regulating one bus have different target voltages.", "Review control grouping, remote terminals and import control arbitration.", {"bus": bid, "targets": group.target_v.to_dict()}, list(map(str, group.index)), "hypothesis")
    for kind in ("lines", "transformers2"):
        for sid, row in tables[kind].iterrows():
            r, x = row.get("r"), row.get("x")
            if not all(finite(v) for v in (r, x)):
                issue("BRANCH_NONFINITE", "Branch has nonfinite R/X.", "Check source impedances and transformer conversion.", sid, row, severity="ERROR")
                continue
            vlid = row.get("voltage_level1_id")
            u = row.get("rated_u2") if kind == "transformers2" else (volts.loc[vlid, "nominal_v"] if vlid in volts.index else None)
            if finite(u) and u > 0:
                zpu = math.hypot(r, x) * 100.0 / u**2
                if zpu < config.get("near_zero_impedance_pu", 1e-7):
                    issue("NEAR_ZERO_IMPEDANCE", f"Branch impedance is {zpu:.3g} pu on 100 MVA base.", "Check intended zero-impedance representation and solver treatment; do not replace automatically.", sid, {**row.to_dict(), "z_pu_100mva": zpu}, "hypothesis")
            # Negative X can be intentional series compensation; it is not categorically invalid.
    for kind in ("ratio_taps", "phase_taps"):
        for sid, row in tables[kind].iterrows():
            if not row.low_tap <= row.tap <= row.high_tap:
                issue("IIDM_TAP_OUT_OF_RANGE", "Imported tap position is outside its range.", "Compare source EQ/SSH tap data and conversion logs.", sid, row, severity="ERROR")
    boundaries = []
    if not boundary.empty:
        for key, group in boundary.groupby("pairing_key", dropna=False):
            if not key: continue
            data = dict(pairing_key=key, ids=list(map(str, group.index)), count=len(group), paired=int(group.paired.sum()),
                        p0_sum_mw=float(group.p0.sum()), q0_sum_mvar=float(group.q0.sum()))
            boundaries.append(data)
            if len(group) > 2:
                f.add("BOUNDARY_PAIRING_AMBIGUOUS", "WARN", scope, "More than two boundary line ends share a pairing key.", "Check boundary ID/name mapping and ownership; multi-terminal arrangements may be intentional.", data, data["ids"], "hypothesis")
            if scope == "CGM" and key in config.get("expected_internal_boundary_keys", []) and not group.paired.all():
                f.add("EXPECTED_BOUNDARY_UNPAIRED", "ERROR", scope, "An explicitly expected internal CGM boundary remains unpaired.", "Check missing neighbor IGM, disconnected terminals and boundary identity mapping.", data, data["ids"])
            if len(group) == 2 and abs(data["p0_sum_mw"]) > config.get("boundary_p_tolerance_mw", 5.0):
                f.add("BOUNDARY_P_DISAGREEMENT", "WARN", scope, "Opposing boundary schedules do not approximately cancel.", "Compare both TSOs' schedule signs/timestamps and boundary representations; losses/conventions can explain part of the difference.", data, data["ids"], "hypothesis")
    # This is a partial scheduled balance, not a solved AC residual. HVDC, shunts, losses and boundary generation are listed as exclusions.
    components = []
    if not buses.empty:
        for (cc, sc), group in buses.groupby(["connected_component", "synchronous_component"]):
            ids = set(group.index)
            def subset(df):
                return df[df.bus_id.isin(ids) & df.connected] if {"bus_id", "connected"}.issubset(df.columns) else df.iloc[0:0]
            def total(df, field): return float(df[field].sum()) if field in df else 0.0
            gg, ll, bb = subset(gens), subset(loads), subset(boundary)
            bat = subset(tables["batteries"])
            # Paired ends are internal flows, and must not be counted as external injections twice.
            bb = bb[~bb.paired] if "paired" in bb else bb
            gen_p, load_p, bd_p, bat_p = total(gg, "target_p"), total(ll, "p0"), total(bb, "p0"), total(bat, "target_p")
            item = dict(connected_component=int(cc), synchronous_component=int(sc), buses=len(group),
                        generators=len(gg), loads=len(ll), scheduled_gen_mw=gen_p, scheduled_load_mw=load_p,
                        unpaired_boundary_export_mw=bd_p, battery_target_mw=bat_p,
                        partial_p_balance_mw=gen_p + bat_p - load_p - bd_p,
                        gen_target_q_mvar=total(gg, "target_q"), load_q_mvar=total(ll, "q0"),
                        interpretation="Partial scheduled balance only: excludes AC losses, shunt Q, HVDC/VSC/LCC, boundary generation and other injections. Regulating-generator target Q is not solved Q.")
            components.append(item)
            controls = int(gg.voltage_regulator_on.sum()) if "voltage_regulator_on" in gg else 0
            alternate_sources = sum(len(subset(tables[k])) for k in ("vsc", "svcs"))
            if len(ll) and controls == 0 and alternate_sources == 0:
                f.add("ISLAND_NO_OBVIOUS_VOLTAGE_SOURCE", "WARN", scope, "A synchronous component with load has no connected regulating generator/VSC/SVC candidate.", "Inspect topology and intended supply/control source, including equivalent/boundary generation; confirm in the load-flow component result.", item, list(map(str, group.index[:20])), "hypothesis")
    if len(components) > 1:
        f.add("MULTIPLE_SYNCHRONOUS_COMPONENTS", "INFO", scope, f"Imported network contains {len(components)} synchronous components.", "Compare with expected islands; multiple synchronous areas and HVDC-separated regions can be legitimate.", {"components": components})
    extra=inspect_extended(n,tables,scope,out,f,config)
    return dict(coverage=coverage, counts={k: len(v) for k, v in tables.items()}, components=components,
                boundaries=boundaries, aliases=record_frame(tables["aliases"]), **extra)


def parameter_kwargs(pp, configured):
    base = dict(voltage_init_mode="UNIFORM_VALUES", transformer_voltage_control_on=True,
                shunt_compensator_voltage_control_on=True, phase_shifter_regulation_on=True,
                use_reactive_limits=True, distributed_slack=True, balance_type="PROPORTIONAL_TO_GENERATION_P_MAX",
                read_slack_bus=False, write_slack_bus=False, component_mode="ALL_CONNECTED",
                provider_parameters={"maxNewtonRaphsonIterations": "30", "maxOuterLoopIterations": "30",
                                     "reportedFeatures": "NEWTON_RAPHSON_LOAD_FLOW"})
    unknown = set(configured) - set(inspect.signature(pp.loadflow.Parameters).parameters)
    if unknown: raise ValueError(f"Unknown load-flow parameters: {sorted(unknown)}")
    for key, parameter in inspect.signature(pp.loadflow.Parameters).parameters.items():
        if key in configured and "bool" in str(parameter.annotation) and not isinstance(configured[key],bool):
            raise ValueError(f"{key} must be a JSON boolean")
    provider=configured.get("provider_parameters",{})
    if not isinstance(provider,dict) or not all(isinstance(v,str) for v in provider.values()):
        raise ValueError("provider_parameters must map parameter names to strings")
    base = merge_parameters(base, configured)
    for key, cls in [("voltage_init_mode", pp.loadflow.VoltageInitMode), ("balance_type", pp.loadflow.BalanceType), ("component_mode", pp.loadflow.ComponentMode)]:
        if isinstance(base.get(key), str): base[key] = getattr(cls, base[key])
    return base


def component_record(r):
    return dict(connected_component=int(r.connected_component_num), synchronous_component=int(r.synchronous_component_num),
                status=r.status.name, status_text=r.status_text, iterations=int(r.iteration_count),
                reference_bus_id=r.reference_bus_id, distributed_active_power_mw=r.distributed_active_power,
                slack_buses=[dict(id=x.id, active_power_mismatch_mw=x.active_power_mismatch) for x in r.slack_bus_results])


def run_validation(pp, n, out, name, f, scope, config, loadflow_parameters=None):
    try:
        params = pp.loadflow.ValidationParameters(check_main_component_only=False,
                    threshold=float(config.get("validation_threshold", 0.1)),
                    loadflow_parameters=loadflow_parameters)
        validation = pp.loadflow.run_validation(n, validation_parameters=params)
        detail = {}
        for attr in ("buses", "generators", "branch_flows", "shunts", "svcs", "twts", "t3wts"):
            frame = getattr(validation, attr)
            if frame is None: continue
            save_frame(out / f"validation-{name}-{attr}.csv", frame)
            bad = frame[~frame.validated.fillna(False)] if "validated" in frame else frame.iloc[0:0]
            detail[attr] = dict(rows=len(frame), failed=len(bad))
            for sid, row in bad.iterrows():
                f.add("AC_CONSISTENCY_FLAG", "WARN", scope, f"{name}: load-flow consistency check flagged {attr}.", "Inspect residual columns, missing values, units and validation tolerances. This flag alone does not prove a source-data error; imported SV can be stale and engine/validator conventions may differ.", {"phase": name, "table": attr, **row.to_dict()}, [str(sid)], certainty="hypothesis")
        return dict(status="completed", valid=bool(validation.valid), tables=detail, parameters=repr(params))
    except Exception as exc:
        f.add("VALIDATION_UNAVAILABLE", "WARN", scope, f"{name} validation failed: {exc}", "Inspect engine report and installed API; no successful validation is implied.")
        return dict(status="failed", error=str(exc))


def worker(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    out, scope, config = Path(job["out"]), job["scope"], job["config"]
    out.mkdir(parents=True, exist_ok=True)
    f = Findings(config.get("max_examples_per_rule", 200))
    result = dict(scope=scope, status="running", stages=[], trials=[], findings=[], finding_counts=[])
    started = time.monotonic()
    def checkpoint():
        result.update(findings=f.items, finding_counts=f.summary(), elapsed_seconds=round(time.monotonic()-started, 3))
        write_json(out / "result.json", result)
    try:
        import pypowsybl as pp
        # Deterministic replay: do not silently inherit the operator workstation's ~/.itools settings.
        pp.set_config_read(False)
        logging.basicConfig(filename=str(out / "engine.log"), level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        result["environment"] = dict(pypowsybl=pp.__version__, python=sys.version, platform=platform.platform(), config_read=False,
                                     provider="OpenLoadFlow", providers=pp.loadflow.get_provider_names())
        if hasattr(pp, "print_version"):
            versions = io.StringIO()
            with contextlib.redirect_stdout(versions): pp.print_version()
            result["environment"]["engine_versions"] = versions.getvalue()
        import_options = config.get("import_parameters", {}).copy()
        available = pp.network.get_import_parameters("CGMES")
        unknown = set(import_options) - set(available.index)
        if unknown: raise ValueError(f"Unknown CGMES import parameters: {sorted(unknown)}")
        # Prevent fallback to unrecorded workstation boundary files; the staging directory holds explicit inputs.
        import_options["iidm.import.cgmes.boundary-location"] = str(Path(job["input"]).parent / "empty-boundary")
        Path(import_options["iidm.import.cgmes.boundary-location"]).mkdir(exist_ok=True)
        effective = {str(k): str(v) for k, v in available["default"].items()}
        effective.update(import_options)
        result["import_parameters"] = effective
        save_frame(out / "available-import-parameters.csv", available)
        save_frame(out / "available-provider-parameters.csv", pp.loadflow.get_provider_parameters("OpenLoadFlow"))
        checkpoint()
        report = pp.report.ReportNode()
        try:
            n = pp.network.load(job["input"], parameters=import_options, report_node=report)
        finally:
            save_report(out, "import-report", report)
            import_report_findings(report, f, scope)
        result["stages"].append(dict(stage="import", status="completed", source_format=n.source_format))
        checkpoint()
        result["network"] = inspect_network(n, scope, out, f, config)
        result["net_positions"]={"imported":area_positions(n,config,f,scope,"imported_state")}
        result["stages"].append(dict(stage="network_checks", status="completed"))
        if job.get("has_sv"):
            result["imported_state_validation"] = run_validation(pp, n, out, "imported-sv", f, scope, config)
        else:
            result["imported_state_validation"] = dict(status="skipped", reason="No SV profile supplied; absent solved state is not an equation failure.")
        checkpoint()
        kwargs = parameter_kwargs(pp, config.get("loadflow_parameters", {}))
        unknown_provider = set(kwargs.get("provider_parameters", {}))-set(pp.loadflow.get_provider_parameters("OpenLoadFlow").index)
        if unknown_provider: raise ValueError(f"Unknown OpenLoadFlow provider parameters: {sorted(unknown_provider)}")
        if kwargs.get("component_mode") != pp.loadflow.ComponentMode.ALL_CONNECTED:
            f.add("COMPONENT_COVERAGE_RESTRICTED", "WARN", scope, "Configured load flow does not cover all connected components.", "Use component_mode=ALL_CONNECTED for full island coverage.")
        pristine = n.get_working_variant_id()
        imported_buses=n.get_buses()
        voltage_state=not imported_buses.empty and imported_buses.v_mag.map(finite).all() and (imported_buses.v_mag>0).all() and imported_buses.v_angle.map(finite).all()
        trials=experiment_plan(pp,config,bool(voltage_state))
        baseline_ok = False
        baseline_components = []
        for spec in trials:
            name,delta=spec["name"],spec["delta"]
            skip=spec.get("skip")
            if name != "baseline_ac" and baseline_ok and not config.get("experiments_on_success", False):skip="Baseline converged; experiments_on_success is false."
            if delta and merge_parameters(kwargs,delta)==kwargs:skip="Parameters already equal the baseline."
            if skip:
                result["trials"].append(dict(name=name,status="skipped",reason=skip,mode=spec["mode"]))
                continue
            n.clone_variant(pristine, name)
            n.set_working_variant(name)
            rep = pp.report.ReportNode()
            trial = dict(name=name, changed_parameters={k: v.name if hasattr(v, "name") else v for k, v in delta.items()}, status="running",mode=spec["mode"])
            result["trials"].append(trial)
            checkpoint()
            t = time.monotonic()
            try:
                if spec.get("control"):
                    trial["control_change"]=disable_control(n,spec["control"])
                params = pp.loadflow.Parameters(**merge_parameters(kwargs, delta))
                trial["effective_parameters"] = repr(params)
                runner=pp.loadflow.run_dc if spec["mode"]=="dc" else pp.loadflow.run_ac
                returned = runner(n, parameters=params, provider="OpenLoadFlow", report_node=rep)
                trial["components"] = [component_record(r) for r in returned]
                trial["all_components_converged"] = bool(returned) and all(r.status.name == "CONVERGED" for r in returned)
                trial["status"] = "completed"
                if name == "baseline_ac":
                    baseline_ok = trial["all_components_converged"]
                    baseline_components = trial["components"]
                    for comp in baseline_components:
                        if comp["status"] != "CONVERGED":
                            f.add("AC_NOT_CONVERGED", "ERROR", scope, f"Baseline AC component status: {comp['status_text']}", "Start with import/topology evidence, then inspect component reference/slack information and controlled experiments.", comp, [comp["reference_bus_id"]] if comp["reference_bus_id"] else [])
                    if not returned:
                        f.add("NO_LOADFLOW_COMPONENTS", "ERROR", scope, "Load flow returned no components.", "Check imported topology and supply sources.")
                    if not baseline_ok:
                        try:
                            failed={(c["connected_component"],c["synchronous_component"]) for c in baseline_components if c["status"]!="CONVERGED"}
                            trial["final_mismatches"] = final_mismatches(rep,n,failed)
                            for mismatch in trial["final_mismatches"]:
                                f.add("SOLVER_FINAL_MISMATCH", "WARN", scope,
                                      f"Final reported {mismatch.get('equationType')} mismatch at bus {mismatch.get('busId')}.",
                                      "Inspect the connected equipment and source changes. A large residual identifies a failing equation, not necessarily the defective equipment.",
                                      mismatch, [mismatch.get("busId","")] + mismatch["connected_equipment"], certainty="hypothesis")
                        except Exception as exc:
                            trial["mismatch_extraction"]={"status":"unavailable","error":str(exc)}
                    if baseline_ok:
                        result["solved_validation"] = run_validation(pp, n, out, "baseline-solved", f, scope, config, params)
                        save_frame(out / "solved-buses.csv", n.get_buses(all_attributes=True))
                        save_frame(out / "solved-generators.csv", n.get_generators(all_attributes=True))
                        inspect_solution(n,scope,f,config)
                        result["net_positions"]["baseline_solved"]=area_positions(n,config,f,scope,"baseline_solved")
                else:
                    was = {(c["connected_component"], c["synchronous_component"]): c["status"] for c in baseline_components}
                    improved = [c for c in trial["components"] if c["status"] == "CONVERGED" and was.get((c["connected_component"], c["synchronous_component"])) not in (None, "CONVERGED")]
                    if spec["mode"]=="dc" and trial["all_components_converged"] and not baseline_ok:
                        f.add("DC_CONVERGED_AC_FAILED","INFO",scope,"The simplified DC model converged while baseline AC did not.","Investigate AC voltage/reactive/control behavior. DC success does not prove AC feasibility or complete/valid topology.",{"experiment":name,"components":trial["components"]},certainty="hypothesis")
                    elif improved:
                        f.add("EXPERIMENT_RESTORED_CONVERGENCE", "WARN", scope, f"Experiment '{name}' converged in a component that failed in baseline.", "Treat this as evidence of sensitivity, not proof of the faulty equipment or an acceptable operational setting. Compare changed control/initialization with source records.", {"experiment": name, "changes": trial["changed_parameters"], "improved_components": improved}, certainty="hypothesis")
            except Exception as exc:
                trial.update(status="failed", error=str(exc))
                f.add("LOADFLOW_EXCEPTION", "ERROR" if name == "baseline_ac" else "WARN", scope, f"{name}: {exc}", "Inspect the saved execution report and baseline parameter compatibility.", {"experiment": name})
            finally:
                trial["elapsed_seconds"] = round(time.monotonic()-t, 3)
                save_report(out, name, rep)
                try:trial["native_events"]=report_events(rep)
                except Exception as exc:trial["native_events_error"]=str(exc)
                n.set_working_variant(pristine)
                n.remove_variant(name)
                checkpoint()
        result.update(status="completed", baseline_converged=baseline_ok)
        result["stages"].append(dict(stage="loadflow", status="completed"))
    except Exception as exc:
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        ids=list(dict.fromkeys(re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",str(exc))))
        f.add("ENGINE_FAILURE", "ERROR", scope, result["error"], "Inspect engine/import reports; compare the installed version and preprocessing with the production tool.",ids=ids)
    checkpoint()
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(worker(sys.argv[1]))
