"""Streaming CIM/XML subset index. This is not an RDF/SHACL conformance validator."""
import hashlib
import math
import re
import shutil
import sqlite3
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from defusedxml import ElementTree as ET

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
MD = "http://iec.ch/TC57/61970-552/ModelDescription/1#"
DCAT = "http://www.w3.org/ns/dcat#"
DM = "http://iec.ch/TC57/61970-552/DifferenceModel/1#"
CIM = {"http://iec.ch/TC57/2013/CIM-schema-cim16#", "http://iec.ch/TC57/CIM100#"}
UUID = re.compile(r"^_?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def local(tag):
    return tag.rsplit("}", 1)[-1].rsplit("#", 1)[-1]


def namespace(tag):
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def identity(value):
    """Normalize CGMES UUID spellings, preserving arbitrary non-UUID URIs/underscores."""
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
    if value.lower().startswith("urn:uuid:"):
        value = value[9:]
    match = UUID.fullmatch(value)
    return match.group(1).lower() if match else value


def profile_kind(uri):
    s = uri.lower()
    if "equipmentboundary" in s: return "EQBD"
    if "topologyboundary" in s: return "TPBD"
    if "steadystatehypothesis" in s: return "SSH"
    if "statevariables" in s: return "SV"
    if "topology" in s: return "TP"
    if "equipmentcore" in s or "coreequipment" in s or re.search(r"/equipment/", s): return "EQ"
    if "equipmentoperation" in s or re.search(r"/operation/", s): return "OP"
    if "equipmentshortcircuit" in s or "shortcircuit" in s: return "SC"
    return "OTHER"


def header_kinds(header):
    return set(header.get("profile_kinds",[])) | {profile_kind(p) for p in header["properties"].get("Model.profile",[])}


class SourceIndex:
    def __init__(self, path, findings):
        self.path = Path(path)
        self.db = sqlite3.connect(path, uri=True)
        self.db.row_factory = sqlite3.Row
        self.f = findings
        self.files = []
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE files(id INTEGER PRIMARY KEY, scope TEXT, source TEXT, staged TEXT, sha256 TEXT, bytes INTEGER);
        CREATE TABLE objects(s TEXT, class TEXT, ns TEXT, file INTEGER);
        CREATE TABLE triples(s TEXT, p TEXT, ns TEXT, v TEXT, kind TEXT, file INTEGER);
        CREATE INDEX obj_s ON objects(s);
        CREATE INDEX obj_c ON objects(class);
        CREATE INDEX tri_s ON triples(s,p);
        CREATE INDEX tri_p ON triples(p);
        CREATE INDEX tri_v ON triples(v) WHERE kind='ref';
        CREATE INDEX tri_mrid ON triples(v) WHERE p='IdentifiedObject.mRID';
        """)

    def evidence(self, subject, predicate=None):
        sql = "SELECT DISTINCT f.scope,f.source,t.p,t.v,t.kind FROM triples t JOIN files f ON f.id=t.file WHERE t.s=?"
        args = [subject]
        if predicate:
            sql += " AND t.p=?"
            args.append(predicate)
        return {"rdf_id": subject, "statements": [dict(r) for r in self.db.execute(sql + " LIMIT 12", args)]}

    def values(self, subject, predicate):
        return [r[0] for r in self.db.execute("SELECT DISTINCT v FROM triples WHERE s=? AND p=?", (subject, predicate))]

    def one(self, subject, predicate, default=None):
        vals = self.values(subject, predicate)
        return vals[0] if len(vals) == 1 else default

    def add_file(self, source, staged, scope):
        with staged.open("rb") as handle:
            sha = hashlib.file_digest(handle, "sha256").hexdigest()
        row = dict(scope=scope, source=source, staged=str(staged), sha256=sha, bytes=staged.stat().st_size)
        cur = self.db.execute("INSERT INTO files(scope,source,staged,sha256,bytes) VALUES(:scope,:source,:staged,:sha256,:bytes)", row)
        fid = cur.lastrowid
        row.update(id=fid, parsed=False, headers=[], namespaces=[])
        self.files.append(row)
        self.db.execute("SAVEPOINT file_parse")
        try:
            depth, root, namespaces = 0, None, set()
            for event, elem in ET.iterparse(str(staged), events=("start", "end"), forbid_dtd=True):
                if event == "start":
                    depth += 1
                    if root is None:
                        root = elem
                        if elem.tag != "{" + RDF + "}RDF":
                            raise ValueError("Expected rdf:RDF root")
                    continue
                if depth == 2:
                    raw_id = elem.get("{" + RDF + "}ID") or elem.get("{" + RDF + "}about")
                    if not raw_id:
                        raise ValueError("Anonymous RDF object: indexed subset requires rdf:ID/about")
                    sid = identity(raw_id)
                    cls, ns = local(elem.tag), namespace(elem.tag)
                    if ns in CIM:
                        namespaces.add(ns)
                    if cls == "Description" and ns == RDF:
                        type_el = elem.find("{" + RDF + "}type")
                        uri = type_el.get("{" + RDF + "}resource", "") if type_el is not None else ""
                        cls = local(uri) if uri else "Description"
                        ns = uri.rsplit("#", 1)[0] + "#" if "#" in uri else ""
                        if ns in CIM: namespaces.add(ns)
                    self.db.execute("INSERT INTO objects VALUES(?,?,?,?)", (sid, cls, ns, fid))
                    triples = []
                    for child in elem:
                        if len(child) or child.get("{" + RDF + "}parseType") or child.get("{" + RDF + "}nodeID"):
                            raise ValueError("Nested/blank-node RDF unsupported by source index; use flat CGMES CIM/XML")
                        if namespace(child.tag) == RDF and local(child.tag) == "type":
                            continue
                        ref = child.get("{" + RDF + "}resource")
                        value = identity(ref) if ref is not None else (child.text or "").strip()
                        triples.append((sid, local(child.tag), namespace(child.tag), value, "ref" if ref is not None else "literal", fid))
                    self.db.executemany("INSERT INTO triples VALUES(?,?,?,?,?,?)", triples)
                    if (cls in ("FullModel", "DifferenceModel") and ns in (MD,DM)) or (cls=="Dataset" and ns==DCAT):
                        props = defaultdict(list)
                        for _, p, _, v, _, _ in triples: props[p].append(v)
                        header=dict(id=sid, kind=cls, properties=dict(props))
                        if cls=="Dataset":
                            header["original_properties"]=dict(props)
                            # Normalize metadata for inspection only; never rewrite native source XML.
                            keywords={v.upper() for v in props.get("keyword",[])}
                            header["profile_kinds"]=["EQBD" if k=="BD" else k for k in keywords if k in {"EQ","SSH","TP","SV","BD","EQBD","TPBD","CD"}]
                            header["properties"]={"Model.profile":props.get("conformsTo",[]),
                                "Model.DependentOn":props.get("requires",[]),"Model.scenarioTime":props.get("startDate",[]),
                                "Model.version":props.get("version",[]),"Dataset.publisher":props.get("publisher",[])}
                        row["headers"].append(header)
                    root.clear()
                depth -= 1
            if not row["headers"]:
                self.f.add("HEADER_MISSING", "ERROR", scope, "CIM/XML file has no recognized FullModel/Dataset header.", "Obtain a complete, correctly headed profile or check the supported metadata encoding.", {"source": source})
            row["namespaces"] = sorted(namespaces)
            row["parsed"] = True
            self.db.execute("RELEASE file_parse")
        except Exception as exc:
            self.db.execute("ROLLBACK TO file_parse")
            self.db.execute("RELEASE file_parse")
            self.f.add("XML_UNREADABLE", "ERROR", scope, str(exc), "Correct the source XML or export supported flat CIM/XML; engine replay is skipped for this scope.", {"source": source})
        self.db.commit()
        return row

    def stage(self, manifest, base, stage, max_bytes):
        """No archive paths are trusted/extracted; copy into generated numeric names."""
        total = 0
        scope_files = defaultdict(list)
        seen_paths = set()
        packages=[(g["name"],g["files"]) for g in manifest.get("igms",[])]
        if manifest.get("cgm"):packages.append(("CGM_PACKAGE",manifest["cgm"]["files"]))
        for scope, entries in [("BOUNDARY", manifest.get("boundaries", [])),("COMMON",manifest.get("common_data",[]))] + packages:
            for entry in entries:
                path = (base / entry).resolve()
                paths = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in (".xml", ".zip")) if path.is_dir() else [path]
                if not paths: raise ValueError(f"No XML/ZIP inputs in {path}")
                for item in paths:
                    key = (scope, str(item))
                    if key in seen_paths: continue
                    seen_paths.add(key)
                    if item.suffix.lower() == ".zip":
                        with zipfile.ZipFile(item) as z:
                            for member in z.infolist():
                                if member.is_dir(): continue
                                if member.filename.lower().endswith(".zip"):
                                    raise ValueError(f"Nested ZIP unsupported: {item}!{member.filename}; unpack outer ZIP locally first")
                                if not member.filename.lower().endswith(".xml"): continue
                                total += member.file_size
                                if total > max_bytes: raise ValueError("Uncompressed inputs exceed max_input_bytes")
                                target = stage / f"profile_{len(self.files):06d}.xml"
                                with z.open(member) as src, target.open("wb") as dst:
                                    self._copy_bounded(src, dst, member.file_size)
                                row = self.add_file(f"{item}!{member.filename}", target, scope)
                                scope_files[scope].append(row)
                    elif item.suffix.lower() == ".xml":
                        total += item.stat().st_size
                        if total > max_bytes: raise ValueError("Inputs exceed max_input_bytes")
                        target = stage / f"profile_{len(self.files):06d}.xml"
                        shutil.copyfile(item, target)
                        scope_files[scope].append(self.add_file(str(item), target, scope))
                    else:
                        raise ValueError(f"Unsupported input: {item}")
        return dict(scope_files)

    @staticmethod
    def _copy_bounded(src, dst, budget):
        copied = 0
        while chunk := src.read(1024 * 1024):
            copied += len(chunk)
            if copied > budget: raise ValueError("ZIP member exceeds declared size")
            dst.write(chunk)

    def check(self, manifest):
        self.headers(manifest)
        for r in self.db.execute("SELECT s,class,file,COUNT(*) n FROM objects GROUP BY s,class,file HAVING n>1"):
            self.f.add("DUPLICATE_DESCRIPTION_IN_FILE","WARN","SOURCE","An RDF object/class is described more than once in one file.","Check duplicate export records. Repetition across separate profiles is expected; formal profile rules decide same-file validity.",dict(r),[r["s"]])
        for r in self.db.execute("SELECT v,GROUP_CONCAT(DISTINCT s) subjects FROM triples WHERE p='IdentifiedObject.mRID' GROUP BY v HAVING COUNT(DISTINCT s)>1"):
            self.f.add("MRID_REUSED","WARN","CGM","Different RDF identities declare the same mRID.","Check identity normalization and equipment ownership before conversion.",dict(r),r["subjects"].split(","),"hypothesis")
        # Only known CIM association predicates: enum URIs and external metadata URIs are not object references.
        associations = {
            "Terminal.ConductingEquipment", "Terminal.ConnectivityNode", "Terminal.TopologicalNode",
            "ConnectivityNode.TopologicalNode", "ConnectivityNode.ConnectivityNodeContainer",
            "TopologicalNode.BaseVoltage", "TopologicalNode.ConnectivityNodeContainer",
            "ConductingEquipment.BaseVoltage", "Equipment.EquipmentContainer", "VoltageLevel.BaseVoltage",
            "VoltageLevel.Substation", "PowerTransformerEnd.PowerTransformer", "TransformerEnd.Terminal",
            "TransformerEnd.BaseVoltage", "TapChanger.TapChangerControl", "RegulatingControl.Terminal",
            "RegulatingCondEq.RegulatingControl", "SynchronousMachine.GeneratingUnit",
            "RatioTapChanger.TransformerEnd", "PhaseTapChanger.TransformerEnd", "OperationalLimit.OperationalLimitSet",
            "OperationalLimit.OperationalLimitType", "OperationalLimitSet.Terminal", "OperationalLimitSet.Equipment",
            "SvVoltage.TopologicalNode", "SvPowerFlow.Terminal", "SvTapStep.TapChanger", "BoundaryPoint.ConnectivityNode", "CommonResponsibilityPoint.ConnectivityNode",
        }
        markers = ",".join("?" for _ in associations)
        sql = f"SELECT t.*, f.scope, f.source FROM triples t JOIN files f ON f.id=t.file WHERE t.kind='ref' AND t.p IN ({markers}) AND NOT EXISTS(SELECT 1 FROM objects o WHERE o.s=t.v)"
        for r in self.db.execute(sql, sorted(associations)):
            if r["ns"] not in CIM and not r["p"].startswith(("CommonResponsibilityPoint.","BoundaryPoint.")): continue
            self.f.add("REFERENCE_MISSING", "ERROR", r["scope"], f"{r['p']} points to an object absent from the supplied package.", "Check model dependencies, boundary version and missing profiles before editing equipment.", dict(r), [r["s"], r["v"]])
        expected={"Terminal.ConnectivityNode":{"ConnectivityNode"},"Terminal.TopologicalNode":{"TopologicalNode"},
                  "VoltageLevel.Substation":{"Substation"},"VoltageLevel.BaseVoltage":{"BaseVoltage"},
                  "ConductingEquipment.BaseVoltage":{"BaseVoltage"},"TransformerEnd.BaseVoltage":{"BaseVoltage"},
                  "TransformerEnd.Terminal":{"Terminal"},"RegulatingControl.Terminal":{"Terminal"},
                  "PowerTransformerEnd.PowerTransformer":{"PowerTransformer"},"SvVoltage.TopologicalNode":{"TopologicalNode"}}
        for predicate,classes in expected.items():
            for r in self.db.execute("SELECT t.*,f.scope,GROUP_CONCAT(DISTINCT o.class) target_classes FROM triples t JOIN files f ON t.file=f.id JOIN objects o ON o.s=t.v WHERE t.p=? AND t.kind='ref' GROUP BY t.s,t.p,t.v,t.file",(predicate,)):
                if r["ns"] in CIM and not classes.intersection(r["target_classes"].split(",")):
                    self.f.add("REFERENCE_TYPE_MISMATCH","ERROR",r["scope"],f"{predicate} resolves to the wrong modeled class.","Check incorrect RDF reference or ID collision; this check covers selected concrete association types only.",dict(r),[r["s"],r["v"]])
        # A repeated RDF ID across EQ/SSH/TP is normal. Conflicting values of selected single-valued properties are not.
        scalar = associations | {"ACLineSegment.r", "ACLineSegment.x", "BaseVoltage.nominalVoltage", "Switch.open",
                  "ACDCTerminal.connected", "EnergyConsumer.p", "EnergyConsumer.q", "RotatingMachine.p", "RotatingMachine.q",
                  "TapChanger.step", "SvVoltage.v", "PowerTransformerEnd.ratedU", "PowerTransformerEnd.ratedS"}
        for r in self.db.execute("SELECT s,p,ns,COUNT(DISTINCT kind||':'||v) n FROM triples GROUP BY s,p,ns HAVING n>1"):
            if r["p"] in scalar and r["ns"] in CIM:
                self.f.add("VALUE_CONFLICT", "ERROR", "CGM", f"Conflicting values for {r['p']} on the same RDF object.", "Select a consistent snapshot/revision or resolve overlapping IGM ownership; do not select an arbitrary value.", self.evidence(r["s"], r["p"]), [r["s"]])
        # Repeated same-class descriptions in distinct profiles are legitimate; cross-IGM equipment ownership is suspicious.
        for r in self.db.execute("""SELECT o.s,o.class,GROUP_CONCAT(DISTINCT f.scope) scopes FROM objects o
            JOIN files f ON f.id=o.file WHERE f.scope!='BOUNDARY' AND o.class IN
            ('ACLineSegment','PowerTransformer','SynchronousMachine','EnergyConsumer','Breaker')
            GROUP BY o.s,o.class HAVING COUNT(DISTINCT f.scope)>1"""):
            self.f.add("OVERLAPPING_IGM_EQUIPMENT", "WARN", "CGM", "The same equipment ID is described by multiple IGMs.", "Confirm ownership and whether this is expected overlap at the border.", dict(r), [r["s"]], "hypothesis")
        self.electrical()

    def headers(self, manifest):
        all_headers = [(f, h) for f in self.files for h in f["headers"] if f["parsed"]]
        ids = {h["id"] for _, h in all_headers}
        by_id, by_scope, time_values = defaultdict(list), defaultdict(set), defaultdict(list)
        graph = {}
        for f, h in all_headers:
            props = h["properties"]
            by_id[h["id"]].append(f)
            graph[h["id"]] = props.get("Model.DependentOn", [])
            profiles = props.get("Model.profile", [])
            kinds = header_kinds(h)
            by_scope[f["scope"]] |= kinds
            if h["kind"] == "DifferenceModel":
                self.f.add("DIFFERENCE_MODEL", "ERROR", f["scope"], "DifferenceModel input requires baseline application before diagnosis.", "Provide the fully materialized snapshot.", {"source": f["source"]})
            if not profiles:
                self.f.add("PROFILE_UNDECLARED", "ERROR", f["scope"], "Header has no Model.profile.", "Correct the model header.", {"source": f["source"]})
            if f["scope"] not in ("BOUNDARY","COMMON") and h["kind"]=="FullModel" and not props.get("Model.modelingAuthoritySet"):
                self.f.add("MODELING_AUTHORITY_MISSING", "WARN", f["scope"], "Modeling authority is absent from a model header.", "Confirm source ownership and how the importer groups CGM subnetworks.", {"source": f["source"], "model": h["id"]})
            for dep in props.get("Model.DependentOn", []):
                if dep not in ids:
                    self.f.add("DEPENDENCY_MISSING", "ERROR", f["scope"], "A declared FullModel dependency is absent.", "Retrieve the exact referenced model/version, including boundary data where applicable.", {"source": f["source"], "model": h["id"], "missing_model": dep})
            if f["scope"] not in ("BOUNDARY","COMMON") and kinds & {"SSH", "TP", "SV"}:
                times = props.get("Model.scenarioTime", [])
                if not times:
                    self.f.add("SCENARIO_TIME_MISSING", "WARN", f["scope"], "State/topology profile does not declare a scenario time.", "Verify that profiles belong to the intended calculation timestamp.", {"source": f["source"]})
                for value in times:
                    try:
                        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        if dt.tzinfo is None: raise ValueError("timezone missing")
                        time_values[dt.timestamp()].append({"scope": f["scope"], "source": f["source"], "time": value})
                    except ValueError:
                        self.f.add("SCENARIO_TIME_INVALID", "ERROR", f["scope"], "Scenario time is invalid or has no timezone.", "Use a timezone-qualified ISO timestamp.", {"source": f["source"], "value": value})
        for sid, files in by_id.items():
            if len({f["sha256"] for f in files}) > 1:
                self.f.add("MODEL_ID_REUSED", "ERROR", "CGM", "Different files declare the same FullModel identifier.", "Resolve duplicate/revised packages before interpreting replay results.", {"model": sid, "files": [f["source"] for f in files]})
        # Explicit DFS stack handles large model sets without Python recursion limits.
        state = {}
        for start in graph:
            if state.get(start): continue
            state[start] = 1
            stack = [(start, iter(graph[start]))]
            while stack:
                node, edges = stack[-1]
                dep = next(edges, None)
                if dep is None:
                    state[node] = 2
                    stack.pop()
                elif dep in graph:
                    if state.get(dep) == 1:
                        self.f.add("DEPENDENCY_CYCLE", "ERROR", "CGM", "FullModel dependencies contain a cycle.", "Correct inconsistent profile dependencies in the exported model headers.", {"from":node,"to":dep})
                    elif not state.get(dep):
                        state[dep] = 1
                        stack.append((dep, iter(graph[dep])))
        if len(time_values) > 1:
            self.f.add("SCENARIO_MISMATCH", "ERROR", "CGM", "State/topology profiles have different scenario times.", "Supply one selected timestamp and revision per IGM; EQ/BD publication dates are intentionally excluded.", {"times": list(time_values.values())})
        namespaces = {ns for f in self.files for ns in f["namespaces"]}
        if len(namespaces) > 1:
            self.f.add("MIXED_CIM_VERSIONS", "ERROR", "CGM", "Both CIM16 and CIM100 data are present.", "Check CGMES versions and convert/select a consistent package.", {"namespaces": sorted(namespaces)})
        expected = manifest.get("required_profiles", ["EQ", "SSH", "TP"])
        packages=manifest.get("igms",[])+([{"name":"CGM_PACKAGE"}] if manifest.get("cgm") else [])
        for group in packages:
            missing = sorted(set(expected) - by_scope[group["name"]])
            if missing:
                self.f.add("PROFILE_MISSING", "ERROR", group["name"], "Required profiles are absent for this IGM.", "Check the delivery package or explicitly adjust required_profiles for the intended stage.", {"missing": missing, "present": sorted(by_scope[group["name"]])})
        # TPBD belongs to older exchange arrangements; never blindly require it for CGMES 3.
        if len(manifest.get("igms",[])) > 1 and "EQBD" not in set().union(*by_scope.values()):
            self.f.add("BOUNDARY_PROFILE_NOT_IDENTIFIED", "WARN", "CGM", "No EquipmentBoundary profile was identified in the multi-IGM input.", "Confirm supplied boundary/common data and their declared dependencies for your exchange specification.", {})

    def electrical(self):
        numeric_suffixes = {"nominalVoltage", "ratedU", "ratedS", "r", "x", "p", "q", "v", "targetValue", "step", "lowStep", "highStep", "minOperatingP", "maxOperatingP"}
        for r in self.db.execute("SELECT t.*,f.scope,f.source FROM triples t JOIN files f ON t.file=f.id WHERE kind='literal'"):
            if r["ns"] not in CIM or r["p"].split(".")[-1] not in numeric_suffixes: continue
            try:
                val = float(r["v"])
                if not math.isfinite(val): raise ValueError()
            except ValueError:
                self.f.add("NONFINITE_OR_INVALID_NUMBER", "ERROR", r["scope"], f"Invalid numeric value for {r['p']}.", "Correct the source value and unit.", dict(r), [r["s"]])
                continue
            if r["p"] in {"BaseVoltage.nominalVoltage", "PowerTransformerEnd.ratedU", "PowerTransformerEnd.ratedS"} and val <= 0:
                self.f.add("NONPOSITIVE_RATING", "ERROR", r["scope"], "Nominal voltage or transformer rating is not positive.", "Check kV/MVA units and the exported rating.", dict(r), [r["s"]])
        for row in self.db.execute("SELECT DISTINCT s,class FROM objects WHERE class IN ('RatioTapChanger','PhaseTapChangerLinear','PhaseTapChangerAsymmetrical','PhaseTapChangerSymmetrical')"):
            sid = row["s"]
            try:
                step, low, high = [float(self.one(sid, p)) for p in ("TapChanger.step", "TapChanger.lowStep", "TapChanger.highStep")]
                if low > high or not low <= step <= high:
                    self.f.add("TAP_OUT_OF_RANGE", "ERROR", "CGM", "SSH tap position is outside EQ tap limits (or limits are inverted).", "Align EQ tap range and SSH tap position; do not clamp automatically.", self.evidence(sid), [sid])
            except (TypeError, ValueError): pass
        for r in self.db.execute("SELECT DISTINCT s FROM objects WHERE class='Terminal'"):
            sid = r[0]
            if not self.values(sid, "Terminal.ConductingEquipment"):
                self.f.add("TERMINAL_EQUIPMENT_MISSING", "ERROR", "CGM", "Terminal has no conducting equipment link.", "Repair the equipment-terminal association in the owning EQ model.", self.evidence(sid), [sid])
            if not (self.values(sid, "Terminal.ConnectivityNode") or self.values(sid, "Terminal.TopologicalNode")):
                self.f.add("TERMINAL_NODE_MISSING", "WARN", "CGM", "Terminal has no connectivity/topological node in supplied profiles.", "Check missing TP data and whether this is an intentionally disconnected terminal.", self.evidence(sid), [sid], "hypothesis")
        for r in self.db.execute("SELECT DISTINCT s FROM objects WHERE class IN ('Breaker','Disconnector','LoadBreakSwitch')"):
            if not self.values(r[0], "Switch.open"):
                self.f.add("SWITCH_STATE_MISSING", "WARN", "CGM", "No SSH open/closed state found for a switch.", "Check SSH completeness; importer fallback to normalOpen/default state can change connectivity.", self.evidence(r[0]), [r[0]], "hypothesis")
        for r in self.db.execute("SELECT DISTINCT s FROM objects WHERE class='PowerTransformerEnd'"):
            sid = r[0]
            transformer = self.one(sid, "PowerTransformerEnd.PowerTransformer")
            terminal = self.one(sid, "TransformerEnd.Terminal")
            equipment = self.one(terminal, "Terminal.ConductingEquipment") if terminal else None
            if transformer and equipment and transformer != equipment:
                self.f.add("TRANSFORMER_END_TERMINAL_CONFLICT", "ERROR", "CGM", "Transformer winding end references a terminal owned by different equipment.", "Correct the EQ transformer-end-terminal association.", self.evidence(sid), [sid, terminal, transformer, equipment])
        # Terminal cardinality only for unambiguous AC two-terminal classes.
        for r in self.db.execute("""SELECT o.s,o.class,COUNT(DISTINCT t.s) n FROM
             (SELECT DISTINCT s,class FROM objects WHERE class IN ('ACLineSegment','Breaker','Disconnector','LoadBreakSwitch')) o
             LEFT JOIN triples t ON t.v=o.s AND t.p='Terminal.ConductingEquipment'
             GROUP BY o.s,o.class HAVING n!=2"""):
            self.f.add("TERMINAL_COUNT", "ERROR", "CGM", f"{r['class']} has {r['n']} distinct terminals; expected two.", "Inspect missing or duplicated terminal associations.", self.evidence(r["s"]), [r["s"]])
        for r in self.db.execute("""SELECT o.s,COUNT(DISTINCT t.s) n FROM
             (SELECT DISTINCT s FROM objects WHERE class='PowerTransformer') o LEFT JOIN triples t
             ON t.v=o.s AND t.p='PowerTransformerEnd.PowerTransformer' GROUP BY o.s HAVING n NOT IN (2,3)"""):
            self.f.add("TRANSFORMER_END_COUNT", "ERROR", "CGM", f"Transformer has {r['n']} winding ends; upstream importer supports two or three.", "Check missing ends or unsupported transformer representation.", self.evidence(r["s"]), [r["s"]])

    def trace(self, iidm_id):
        sid = identity(str(iidm_id))
        ids = {sid}
        for row in self.db.execute("SELECT s FROM triples WHERE p='IdentifiedObject.mRID' AND v IN (?,?)", (str(iidm_id), sid)):
            ids.add(row[0])
        result = []
        for key in sorted(ids):
            for r in self.db.execute("""SELECT DISTINCT o.s,o.class,f.scope,f.source,f.id AS file_id FROM objects o JOIN files f ON f.id=o.file WHERE o.s=? LIMIT 12""", (key,)):
                item = dict(r)
                original = self.files[item["file_id"]-1]
                item["models"] = [{"id":h["id"], "profiles":h["properties"].get("Model.profile", []),
                    "authority":h["properties"].get("Model.modelingAuthoritySet", []),
                    "publisher":h["properties"].get("Dataset.publisher", []),
                    "scenario_time":h["properties"].get("Model.scenarioTime", []),
                    "version":h["properties"].get("Model.version", [])} for h in original["headers"]]
                result.append(item)
        return result

    def compare(self, previous_path, csv_path):
        from .common import write_csv
        # Previous DB is attached read-only; compare semantic values, not file names/model header UUIDs.
        uri = Path(previous_path).resolve().as_uri() + "?mode=ro"
        self.db.execute("ATTACH DATABASE ? AS prior", (uri,))
        sql = """SELECT s,p,ns,v,kind FROM {a}.triples WHERE ns IN (?,?)
                 EXCEPT SELECT s,p,ns,v,kind FROM {b}.triples WHERE ns IN (?,?)"""
        counts = {}
        def rows():
            for change, a, b in [("added", "main", "prior"), ("removed", "prior", "main")]:
                counts[change] = 0
                for r in self.db.execute(sql.format(a=a, b=b), (*sorted(CIM), *sorted(CIM))):
                    counts[change] += 1
                    yield dict(change=change, **dict(r))
        write_csv(csv_path, rows(), ["change", "s", "p", "ns", "v", "kind"])
        self.db.execute("DETACH DATABASE prior")
        return counts

    def close(self):
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()
