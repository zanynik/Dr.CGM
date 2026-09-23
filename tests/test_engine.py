import hashlib
import json
from pathlib import Path
import pytest
pytest.importorskip("pypowsybl")
from make_demo import generate
from cgmes_diag.cli import main


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("engine-cases")/"inputs")


def read(folder):return json.loads((folder/"report.json").read_text())


def test_real_cgmes3_import_loadflow_and_trials_preserve_inputs(cases,tmp_path):
    manifest=json.loads((cases/"healthy.json").read_text())
    manifest["igms"][0]["files"]=[str(cases/"healthy.zip")]
    manifest["engine"]["experiments_on_success"]=True
    m=tmp_path/"case.json";m.write_text(json.dumps(manifest))
    before=hashlib.sha256((cases/"healthy.zip").read_bytes()).hexdigest()
    out=tmp_path/"report"
    assert main(["--manifest",str(m),"--out",str(out)])==0
    report=read(out)
    assert report["status"]=="completed"
    assert all(x["baseline_converged"] for x in report["engines"])
    trials=report["engines"][0]["trials"]
    assert len(trials)==14
    assert {"previous_values","full_voltage_initialization","generation_p_balance","dc_loadflow"}<={t["name"] for t in trials}
    assert all(t["status"] in {"completed","skipped"} for e in report["engines"] for t in e["trials"])
    assert next(t for t in trials if t["name"]=="dc_loadflow")["mode"]=="dc"
    assert hashlib.sha256((cases/"healthy.zip").read_bytes()).hexdigest()==before
    assert (out/"engine/CGM/baseline_ac.json").exists()


def test_broken_source_is_attributed_before_solver(cases,tmp_path):
    out=tmp_path/"broken"
    assert main(["--manifest",str(cases/"broken-source.json"),"--out",str(out),"--preflight-only"])==1
    report=read(out)
    rules={f["code"] for f in report["findings"]}
    assert {"REFERENCE_MISSING","SCENARIO_MISMATCH","NONPOSITIVE_RATING"}<=rules
    assert any("broken-source.zip" in str(f["evidence"]) for f in report["findings"])


def test_two_igms_all_components_are_replayed(cases,tmp_path):
    out=tmp_path/"two"
    code=main(["--manifest",str(cases/"two-islands.json"),"--out",str(out)])
    assert code==0
    cgm=next(x for x in read(out)["engines"] if x["scope"]=="CGM")
    assert cgm["baseline_converged"]
    assert len(cgm["trials"][0]["components"])==2


def test_stressed_case_exposes_real_nonconvergence(cases,tmp_path):
    out=tmp_path/"stressed"
    assert main(["--manifest",str(cases/"stressed.json"),"--out",str(out)])==1
    report=read(out)
    assert any(f["code"]=="AC_NOT_CONVERGED" for f in report["findings"])
    assert any(f["code"]=="SOLVER_FINAL_MISMATCH" and f.get("source_trace") for f in report["findings"])
    assert all(len(e["trials"])>1 for e in report["engines"])


def test_worker_timeout_is_reported_as_incomplete(cases,tmp_path):
    manifest=json.loads((cases/"healthy.json").read_text())
    manifest["igms"][0]["files"]=[str(cases/"healthy.zip")]
    manifest["engine"]["timeout_seconds"]=0.001
    m=tmp_path/"case.json";m.write_text(json.dumps(manifest))
    out=tmp_path/"timeout"
    assert main(["--manifest",str(m),"--out",str(out)])==2
    assert read(out)["status"]=="incomplete"


def test_native_boundary_pairing_distinguishes_expected_internal_from_external(tmp_path):
    import pypowsybl as pp
    from cgmes_diag.engine import inspect_network
    from cgmes_diag.common import Findings
    n=pp.network.create_eurostag_tutorial_example1_with_tie_lines_and_areas()
    n.remove_elements(['NHV1_NHV2_1'])
    expected=Findings()
    a=tmp_path/'expected';a.mkdir()
    inspect_network(n,'CGM',a,expected,{'expected_internal_boundary_keys':['XNODE1']})
    assert any(x['code']=='EXPECTED_BOUNDARY_UNPAIRED' for x in expected.items)
    external=Findings()
    b=tmp_path/'external';b.mkdir()
    inspect_network(n,'CGM',b,external,{})
    assert not any(x['code']=='EXPECTED_BOUNDARY_UNPAIRED' for x in external.items)
