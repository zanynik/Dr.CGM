import json
import zipfile
from pathlib import Path
import pytest
from cgmes_diag.source import SourceIndex, identity, profile_kind
from cgmes_diag.common import Findings
from cgmes_diag.cli import main, validate_manifest

RDF="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
CIM="http://iec.ch/TC57/CIM100#"
MD="http://iec.ch/TC57/61970-552/ModelDescription/1#"


def xml(body="", model="model", profile="CoreEquipment", time="2026-09-23T12:00:00Z", dependency=""):
    return f'''<rdf:RDF xmlns:rdf="{RDF}" xmlns:cim="{CIM}" xmlns:md="{MD}">
    <md:FullModel rdf:about="urn:uuid:{model}"><md:Model.profile>http://iec.ch/TC57/ns/CIM/{profile}-EU/3.0</md:Model.profile><md:Model.scenarioTime>{time}</md:Model.scenarioTime>
    <md:Model.modelingAuthoritySet>urn:tso:A</md:Model.modelingAuthoritySet>{dependency}</md:FullModel>{body}</rdf:RDF>'''


@pytest.fixture
def index(tmp_path):
    f=Findings(); idx=SourceIndex(tmp_path/"index.sqlite",f)
    yield idx,f,tmp_path
    idx.close()


def add(idx,tmp,name,text):
    path=tmp/name;path.write_text(text);return idx.add_file(name,path,"A")


def check(idx):
    idx.check({"igms":[{"name":"A","files":["ignored"]}],"required_profiles":["EQ"]})


def codes(f): return {x["code"] for x in f.items}


def test_uuid_spelling_is_normalized_without_collapsing_non_uuid_ids():
    value="f6adc344-32f8-4a3a-bd09-765c8fde658f"
    assert identity("#_"+value)==identity("urn:uuid:"+value.upper())
    assert identity("#_line")!="line"
    assert identity("https://example.com/resource#_line")!="_line"


def test_cross_profile_same_id_is_normal_and_enums_are_not_missing_references(index):
    idx,f,tmp=index
    add(idx,tmp,"eq.xml",xml('<cim:SynchronousMachine rdf:ID="_g"><cim:SynchronousMachine.type rdf:resource="http://iec.ch/TC57/CIM100#SynchronousMachineKind.generator"/></cim:SynchronousMachine>'))
    add(idx,tmp,"ssh.xml",xml('<cim:SynchronousMachine rdf:about="#_g"><cim:RotatingMachine.p>-100</cim:RotatingMachine.p></cim:SynchronousMachine>',"ssh","SteadyStateHypothesis"))
    check(idx)
    assert not codes(f)&{"REFERENCE_MISSING","VALUE_CONFLICT","MODEL_ID_REUSED","PROFILE_MISSING"}
    assert len(idx.trace("_g"))==2


def test_missing_link_and_conflicting_values_are_found(index):
    idx,f,tmp=index
    add(idx,tmp,"eq.xml",xml('<cim:ACLineSegment rdf:ID="_l"><cim:ACLineSegment.r>2</cim:ACLineSegment.r></cim:ACLineSegment><cim:Terminal rdf:ID="_t"><cim:Terminal.ConductingEquipment rdf:resource="#_missing"/></cim:Terminal>'))
    add(idx,tmp,"extra.xml",xml('<cim:ACLineSegment rdf:about="#_l"><cim:ACLineSegment.r>3</cim:ACLineSegment.r></cim:ACLineSegment>',"model2"))
    check(idx)
    assert {"REFERENCE_MISSING","VALUE_CONFLICT","TERMINAL_COUNT"} <= codes(f)


def test_eq_time_ignored_and_equivalent_timezone_is_same_instant(index):
    idx,f,tmp=index
    add(idx,tmp,"eq.xml",xml(time="2001-01-01T00:00:00Z"))
    add(idx,tmp,"ssh.xml",xml(model="ssh",profile="SteadyStateHypothesis",time="2026-09-23T14:00:00+02:00"))
    add(idx,tmp,"tp.xml",xml(model="tp",profile="Topology"))
    check(idx)
    assert "SCENARIO_MISMATCH" not in codes(f)
    assert "PROFILE_MISSING" not in codes(f)
    assert profile_kind("http://iec.ch/TC57/ns/CIM/EquipmentBoundary-EU/3.0")=="EQBD"


def test_dependencies_missing_and_cycles(index):
    idx,f,tmp=index
    add(idx,tmp,"a.xml",xml(model="a",dependency='<md:Model.DependentOn rdf:resource="urn:uuid:b"/>'))
    add(idx,tmp,"b.xml",xml(model="b",dependency='<md:Model.DependentOn rdf:resource="urn:uuid:a"/><md:Model.DependentOn rdf:resource="urn:uuid:c"/>'))
    check(idx)
    assert {"DEPENDENCY_CYCLE","DEPENDENCY_MISSING"} <= codes(f)


def test_malformed_xml_rolls_back_all_partial_triples(index):
    idx,f,tmp=index
    text=xml('<cim:BaseVoltage rdf:ID="_v"><cim:BaseVoltage.nominalVoltage>400</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage>')[:-4]
    row=add(idx,tmp,"bad.xml",text)
    assert not row["parsed"]
    assert idx.db.execute("SELECT COUNT(*) FROM triples").fetchone()[0]==0
    assert "XML_UNREADABLE" in codes(f)


def test_entities_are_rejected(index):
    idx,f,tmp=index
    row=add(idx,tmp,"xxe.xml",'<!DOCTYPE rdf:RDF [<!ENTITY x SYSTEM "file:///etc/passwd">]>'+xml('&x;'))
    assert not row["parsed"]
    assert "XML_UNREADABLE" in codes(f)


def test_archive_members_cannot_escape_staging(index):
    idx,f,tmp=index
    archive=tmp/"input.zip"
    with zipfile.ZipFile(archive,"w") as z:z.writestr("../../outside.xml",xml())
    stage=tmp/"stage";stage.mkdir()
    idx.stage({"igms":[{"name":"A","files":["input.zip"]}]},tmp,stage,100000)
    assert (stage/"profile_000000.xml").exists()
    assert not (tmp/"outside.xml").exists()


def test_numeric_and_tap_failures(index):
    idx,f,tmp=index
    add(idx,tmp,"eq.xml",xml('<cim:BaseVoltage rdf:ID="_v"><cim:BaseVoltage.nominalVoltage>0</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage><cim:RatioTapChanger rdf:ID="_tap"><cim:TapChanger.lowStep>-5</cim:TapChanger.lowStep><cim:TapChanger.highStep>5</cim:TapChanger.highStep><cim:TapChanger.step>7</cim:TapChanger.step></cim:RatioTapChanger><cim:EnergyConsumer rdf:ID="_load"><cim:EnergyConsumer.p>NaN</cim:EnergyConsumer.p></cim:EnergyConsumer>'))
    check(idx)
    assert {"TAP_OUT_OF_RANGE","NONPOSITIVE_RATING","NONFINITE_OR_INVALID_NUMBER"}<=codes(f)


def test_cli_preflight_diff_and_escaped_html(tmp_path):
    x=tmp_path/"eq.xml";x.write_text(xml('<cim:BaseVoltage rdf:ID="_v"><cim:BaseVoltage.nominalVoltage>400</cim:BaseVoltage.nominalVoltage></cim:BaseVoltage>'))
    m=tmp_path/"manifest.json";m.write_text(json.dumps({"igms":[{"name":"A","files":["eq.xml"]}],"required_profiles":["EQ"]}))
    assert main(["--manifest",str(m),"--out",str(tmp_path/"r1"),"--preflight-only"])==0
    x.write_text(x.read_text().replace(">400<",">0<"))
    assert main(["--manifest",str(m),"--out",str(tmp_path/"r2"),"--preflight-only","--previous",str(tmp_path/"r1")])==1
    result=json.loads((tmp_path/"r2/report.json").read_text())
    assert result["comparison"]=={"added":1,"removed":1}
    assert "preflight only" in (tmp_path/"r2/report.html").read_text()


def test_cap_preserves_true_counts():
    f=Findings(2)
    for _ in range(10): f.add("X","ERROR","A","x","y")
    assert len(f.items)==2 and f.summary()[0]["count"]==10


def test_unsupported_config_does_not_silently_pass():
    with pytest.raises(ValueError):validate_manifest({"igms":[{"name":"A","files":["x"]}],"engine":{"typo":True}})


def test_dataset_boundary_header_dependencies_and_source_attribution(index):
    idx,f,tmp=index
    add(idx,tmp,"eq.xml",xml())
    text=f'''<rdf:RDF xmlns:rdf="{RDF}" xmlns:dcat="http://www.w3.org/ns/dcat#" xmlns:dct="http://purl.org/dc/terms/">
    <dcat:Dataset rdf:about="urn:uuid:boundary"><dcat:keyword>BD</dcat:keyword><dct:conformsTo rdf:resource="{CIM}"/><dct:requires rdf:resource="urn:uuid:model"/></dcat:Dataset></rdf:RDF>'''
    p=tmp/"bd.xml";p.write_text(text)
    row=idx.add_file("bd.xml",p,"BOUNDARY")
    check(idx)
    assert row["headers"][0]["profile_kinds"]==["EQBD"]
    assert not codes(f)&{"HEADER_MISSING","DEPENDENCY_MISSING","PROFILE_UNDECLARED"}


def test_wrong_reference_type_and_duplicate_mrid_are_diagnosed(index):
    idx,f,tmp=index
    body='<cim:Terminal rdf:ID="_t"><cim:Terminal.ConnectivityNode rdf:resource="#_load"/></cim:Terminal><cim:EnergyConsumer rdf:ID="_load"><cim:IdentifiedObject.mRID>same</cim:IdentifiedObject.mRID></cim:EnergyConsumer><cim:EnergyConsumer rdf:ID="_load2"><cim:IdentifiedObject.mRID>same</cim:IdentifiedObject.mRID></cim:EnergyConsumer>'
    add(idx,tmp,"eq.xml",xml(body));check(idx)
    assert {"REFERENCE_TYPE_MISMATCH","MRID_REUSED"}<=codes(f)
