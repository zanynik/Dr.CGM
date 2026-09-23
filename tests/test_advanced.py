import json
from pathlib import Path
import pytest
pp=pytest.importorskip("pypowsybl")
from cgmes_diag.common import Findings
from cgmes_diag.experiments import merge_parameters, disable_control
from cgmes_diag.advanced import area_positions, inspect_solution
from cgmes_diag.cli import main, validate_manifest
from make_demo import generate
from prepare_relicapgrid import build


def test_provider_override_preserves_other_settings_and_input():
    base={"provider_parameters":{"maxOuterLoopIterations":"11","fixVoltageTargets":"false"}}
    merged=merge_parameters(base,{"provider_parameters":{"fixVoltageTargets":"true"}})
    assert merged["provider_parameters"]=={"maxOuterLoopIterations":"11","fixVoltageTargets":"true"}
    assert base["provider_parameters"]["fixVoltageTargets"]=="false"
    from cgmes_diag.engine import parameter_kwargs
    kwargs=parameter_kwargs(pp,{"provider_parameters":{"maxOuterLoopIterations":"11"}})
    assert kwargs["provider_parameters"]["reportedFeatures"]=="NEWTON_RAPHSON_LOAD_FLOW"
    with pytest.raises(ValueError):parameter_kwargs(pp,{"distributed_slack":"false"})


def test_selected_control_trial_does_not_mutate_imported_variant():
    n=pp.network.create_eurostag_tutorial_example1_with_tie_lines_and_areas()
    pristine=n.get_working_variant_id()
    n.clone_variant(pristine,"test");n.set_working_variant("test")
    assert disable_control(n,{"kind":"generator","element_id":"GEN"})["before"]
    assert not n.get_generators().loc["GEN","voltage_regulator_on"]
    n.set_working_variant(pristine)
    assert n.get_generators().loc["GEN","voltage_regulator_on"]
    with pytest.raises(ValueError):disable_control(n,{"kind":"generator","element_id":"missing"})


def test_generator_setpoint_faults_are_detected(tmp_path):
    from cgmes_diag.engine import inspect_network
    n=pp.network.create_ieee14()
    ids=n.get_generators().index
    n.update_generators(id=ids[0],target_p=10000.,target_q=50.,voltage_regulator_on=False,min_q=-1.,max_q=1.)
    n.update_generators(id=ids[1],target_v=10000.)
    f=Findings();inspect_network(n,"TEST",tmp_path,f,{})
    assert {"GENERATOR_P_LIMIT","FIXED_Q_OUTSIDE_LIMITS","VOLTAGE_TARGET_IMPLAUSIBLE"}<={r["code"] for r in f.items}


def test_area_positions_require_flows_and_use_native_boundary_interchange():
    from types import SimpleNamespace
    n=pp.network.create_eurostag_tutorial_example1_with_tie_lines_and_areas()
    f=Findings()
    missing=n.get_areas_boundaries(all_attributes=True).copy()
    missing["p"]=float("nan")
    unsolved=SimpleNamespace(get_areas=n.get_areas,get_areas_boundaries=lambda **kw:missing)
    before=area_positions(unsolved,{},f,"CGM","imported")
    assert all(r["difference_mw"] is None for r in before)
    assert all(r.status.name=="CONVERGED" for r in pp.loadflow.run_ac(n))
    rows=area_positions(n,{"area_net_position_targets_mw":{"ControlArea_A":0.0}},f,"CGM","solved")
    area=next(r for r in rows if r["id"]=="ControlArea_A")
    assert area["complete_boundary_flows"]
    assert area["difference_mw"]==pytest.approx(n.get_areas().loc["ControlArea_A","interchange"])
    assert any(r["code"]=="AREA_NET_POSITION_DEVIATION" for r in f.items)


def test_manifest_rejects_mixed_pipeline_stages_and_string_measurements():
    with pytest.raises(ValueError):validate_manifest({"igms":[{"name":"A","files":["a"]}],"cgm":{"files":["b"]}})
    with pytest.raises(ValueError):validate_manifest({"cgm":{"files":[42]}})
    with pytest.raises(ValueError):validate_manifest({"cgm":{"files":["a"]},"alignment_observations":[{"area":"A","source":"s","original_mw":"3","target_mw":2,"aligned_mw":1}]})


def test_alignment_evidence_and_bounded_subset_replay(tmp_path):
    inputs=generate(tmp_path/"inputs")
    manifest=json.loads((inputs/"two-islands.json").read_text())
    manifest["igms"][0]["files"]=[str(inputs/"stressed.zip")]
    manifest["igms"][1]["files"]=[str(inputs/"area-b.zip")]
    manifest["engine"]["experiments"]=False
    manifest["localization"]={"enabled":True,"max_runs":1,"subsets":[["AREA_B"],["AREA_A"]]}
    manifest["alignment_observations"]=[{"area":"A","original_mw":50.,"target_mw":80.,"aligned_mw":70.,"source":"synthetic test","tolerance_mw":5.}]
    path=tmp_path/"case.json";path.write_text(json.dumps(manifest))
    out=tmp_path/"report"
    assert main(["--manifest",str(path),"--out",str(out)])==1
    data=json.loads((out/"report.json").read_text())
    assert data["localization"]["untested"]==1
    assert len(data["localization"]["runs"])==1
    assert data["localization"]["runs"][0]["baseline_converged"]
    codes={f["code"] for f in data["findings"]}
    assert {"ALIGNMENT_TARGET_MISSED","REGION_OMISSION_RESTORED_CONVERGENCE"}<=codes
    assert data["alignment_observations"][0]["required_change_mw"]==30.


def test_relicap_builder_selects_updated_state_without_original_ssh(tmp_path):
    paths=["A/Grid/cimxml/case_A_EQ_1.xml","A/Grid/cimxml/case_A_SSH_1.xml",
           "Jotunheim/Grid/cimxml/case_Jotunheim_TP_1.xml","Jotunheim/Grid/cimxml/case_Jotunheim_SV_1.xml",
           "Jotunheim/ChangeSet/cimxml/case_A_SSH_2.xml","boundaryData/Grid/cimxml/BD.xml",
           "commonData/Grid/cimxml/Grid_CommonData_CGM-CD.xml"]
    for path in paths:
        p=tmp_path/"Instance"/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("fixture")
    manifest=build(tmp_path)
    assert any("SSH_2" in p for p in manifest["cgm"]["files"])
    assert not any("SSH_1" in p for p in manifest["cgm"]["files"])
    validate_manifest(manifest)
    (tmp_path/"Instance"/paths[4]).unlink()
    with pytest.raises(ValueError):build(tmp_path)
