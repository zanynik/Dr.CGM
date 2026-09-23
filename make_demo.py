#!/usr/bin/env python3
"""Generate reproducible synthetic CGMES 3.0 datasets; never reads operational data."""
import argparse
import json
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
CIM = "http://iec.ch/TC57/CIM100#"
MD = "http://iec.ch/TC57/61970-552/ModelDescription/1#"
for prefix, uri in [("rdf",RDF),("cim",CIM),("md",MD),("eu","http://iec.ch/TC57/CIM100-European#")]:
    ET.register_namespace(prefix,uri)


def manifest(out, name, files, **config):
    value = {"igms":[{"name":label,"files":[path]} for label,path in files],"boundaries":[],
             "engine":{"timeout_seconds":90,"experiments":True,**config}}
    (out/(name+".json")).write_text(json.dumps(value,indent=2),encoding="utf-8")


def generate(out):
    import pypowsybl as pp
    pp.set_config_read(False)
    out.mkdir(parents=True,exist_ok=False)
    n = pp.network.create_ieee14()
    pp.loadflow.run_ac(n)
    n.save(out/"healthy.zip","CGMES",{"iidm.export.cgmes.cim-version":"100","iidm.export.cgmes.modeling-authority-set":"urn:demo:AREA_A"})
    with zipfile.ZipFile(out/"healthy.zip") as src:
        for case in ("broken-source","stressed","area-b"):
            with zipfile.ZipFile(out/(case+".zip"),"w",zipfile.ZIP_DEFLATED) as dst:
                for name in src.namelist():
                    root=ET.fromstring(src.read(name))
                    if case=="broken-source":
                        if name.endswith("_EQ.xml"):
                            for el in root.iter("{"+CIM+"}BaseVoltage.nominalVoltage"):
                                el.text="0";break
                            for el in root.iter("{"+CIM+"}Terminal.ConductingEquipment"):
                                el.set("{"+RDF+"}resource","#_MISSING_EQUIPMENT");break
                        if name.endswith("_SSH.xml"):
                            for el in root.iter("{"+MD+"}Model.scenarioTime"): el.text="1993-08-19T01:00:00Z"
                    elif case=="stressed":
                        if name.endswith("_SSH.xml"):
                            for el in root.iter():
                                if el.tag in ("{"+CIM+"}EnergyConsumer.p","{"+CIM+"}EnergyConsumer.q"):
                                    el.text=str(float(el.text)*10)
                    else:
                        # Independent second area: rewrite IDs/references, preserving enum/schema URIs.
                        for el in root.iter():
                            for key in ("ID","about","resource"):
                                attr="{"+RDF+"}"+key
                                if attr not in el.attrib: continue
                                value=el.attrib[attr]
                                if value.startswith("urn:uuid:"): el.set(attr,"urn:uuid:AREA_B_"+value[9:])
                                elif value.startswith("#_"): el.set(attr,"#_AREA_B_"+value[2:])
                                elif key=="ID" and value.startswith("_"): el.set(attr,"_AREA_B_"+value[1:])
                            if el.tag=="{"+CIM+"}IdentifiedObject.mRID": el.text="AREA_B_"+(el.text or "")
                            if el.tag=="{"+MD+"}Model.modelingAuthoritySet": el.text="urn:demo:AREA_B"
                    dst.writestr(name,ET.tostring(root,encoding="utf-8",xml_declaration=True))
    manifest(out,"healthy",[("AREA_A","healthy.zip")])
    manifest(out,"broken-source",[("AREA_A","broken-source.zip")])
    manifest(out,"stressed",[("AREA_A","stressed.zip")])
    manifest(out,"two-islands",[("AREA_A","healthy.zip"),("AREA_B","area-b.zip")])
    (out/"README.txt").write_text("Synthetic IEEE 14-bus CGMES 3.0 export generated using PyPowSyBl.\nhealthy: baseline solved case.\nbroken-source: zero base voltage, missing equipment reference, mixed state timestamps.\nstressed: load P and Q multiplied by ten; retained SV is deliberately stale.\ntwo-islands: two independent copies to verify multi-IGM import/all-component coverage; NO cross-border pairing is represented.\nThese are engineering tests, not ENTSO-E conformity test cases.\n",encoding="utf-8")
    return out


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--out",type=Path,default=Path("demo-input"));args=p.parse_args()
    generate(args.out.resolve());print(f"Created {args.out}")
