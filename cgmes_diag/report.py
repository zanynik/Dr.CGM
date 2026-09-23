import html
from collections import Counter
from pathlib import Path
from .common import write_csv, write_json


def h(value): return html.escape(str(value))


def table(records, columns):
    if not records: return "<p>No records supplied or available for this stage.</p>"
    heads="".join(f"<th>{h(label)}</th>" for _,label in columns)
    rows="".join("<tr>"+"".join(f"<td>{h(row.get(key,''))}</td>" for key,_ in columns)+"</tr>" for row in records)
    return f'<div class="scroll"><table><thead><tr>{heads}</tr></thead><tbody>{rows}</tbody></table></div>'


def render(out, data):
    write_json(out / "report.json", data)
    priorities = {"XML_UNREADABLE":0,"DEPENDENCY_MISSING":1,"DEPENDENCY_CYCLE":1,"PROFILE_MISSING":2,
                  "MODEL_ID_REUSED":3,"SCENARIO_MISMATCH":3,"MIXED_CIM_VERSIONS":3,
                  "REFERENCE_MISSING":4,"VALUE_CONFLICT":4,"NONPOSITIVE_RATING":5,
                  "NONFINITE_OR_INVALID_NUMBER":5,"ENGINE_FAILURE":20,"AC_NOT_CONVERGED":30}
    findings = sorted(data["findings"], key=lambda x: ({"ERROR":0,"WARN":1,"INFO":2}.get(x["severity"],3), priorities.get(x["code"],15), x["code"], x["scope"]))
    fields = ["severity", "code", "scope", "certainty", "message", "action", "ids", "evidence", "source_trace"]
    write_csv(out / "findings.csv", findings, fields)
    summary = Counter()
    for item in data["finding_counts"]: summary[item["severity"]] += item["count"]
    cards = "".join(f'<div class="metric"><b>{summary.get(k,0):,}</b><span>{label}</span></div>' for k,label in [("ERROR","Errors"),("WARN","Warnings"),("INFO","Information")])
    scopes = []
    for item in data.get("engines", []):
        base = next((t for t in item.get("trials", []) if t["name"] == "baseline_ac"), {})
        components = base.get("components", [])
        status = " / ".join(f"CC {c['connected_component']} SC {c['synchronous_component']}: {c['status']}" for c in components) or item.get("reason", item.get("error", "No AC result"))
        path = item.get("directory")
        link = f'<a href="{h(path)}/result.json">Details</a>' if path else ""
        if path and (out/path/"import-report.txt").exists():link+=f' · <a href="{h(path)}/import-report.txt">Import report</a>'
        scopes.append(f'<tr><td>{h(item["scope"])}</td><td>{h(item["status"])}</td><td>{h(status)}</td><td>{link}</td></tr>')
    rows = []
    import json
    for item in findings:
        ev = json.dumps({"evidence":item.get("evidence",{}),"source_trace":item.get("source_trace",[])}, indent=2, ensure_ascii=False)
        rows.append(f'''<article class="finding {h(item['severity'].lower())}" data-severity="{h(item['severity'])}">
        <div class="meta"><span class="badge">{h(item['severity'])}</span> {h(item['scope'])} · {h(item['code'])} · {h(item['certainty'])}</div>
        <h3>{h(item['message'])}</h3><p><strong>Next check:</strong> {h(item['action'])}</p>
        <p class="ids">{h(', '.join(item.get('ids',[])))}</p><details><summary>Evidence & source attribution</summary><pre>{h(ev)}</pre></details></article>''')
    comparison = data.get("comparison")
    diff = f'<p>Semantic changes: {h(comparison)}. <a href="changes.csv">Open changed statements</a>.</p>' if comparison else ""
    if data.get("configuration_comparison"):
        diff+=f'<details><summary>Changed diagnostic configuration</summary><pre>{h(json.dumps(data["configuration_comparison"],indent=2))}</pre></details>'
    trials=[];positions=[]
    for engine in data.get("engines",[]):
        for trial in engine.get("trials",[]):
            path=f'{engine.get("directory","")}/{trial["name"]}.txt'
            details=f'<a href="{h(path)}">Native report</a>' if (out/path).exists() else ""
            outcomes="; ".join(f'CC {c["connected_component"]}/SC {c["synchronous_component"]}: {c["status"]}' for c in trial.get("components",[]))
            trials.append(f'<tr><td>{h(engine["scope"])}</td><td>{h(trial["name"])}</td><td>{h(trial.get("mode","ac"))}</td><td>{h(trial["status"])}</td><td>{h(trial.get("reason",trial.get("error",outcomes)))}</td><td>{details}</td></tr>')
        for stage,records in engine.get("net_positions",{}).items():
            positions.extend({"scope":engine["scope"],**record} for record in records)
    trial_html='<div class="scroll"><table><thead><tr><th>Scope</th><th>Trial</th><th>Mode</th><th>Execution</th><th>Outcome / coverage</th><th>Evidence</th></tr></thead><tbody>'+''.join(trials)+'</tbody></table></div>'
    position_html=table(positions,[("scope","Scope"),("id","Area"),("phase","Stage"),("target_mw","Target MW"),("interchange_mw","Boundary interchange MW"),("difference_mw","Difference MW"),("complete_boundary_flows","Boundary flows available")])
    alignment_html=table(data.get("alignment_observations",[]),[("area","Area"),("original_mw","Original MW"),("target_mw","Target MW"),("aligned_mw","After alignment MW"),("required_change_mw","Required change MW"),("applied_change_mw","Applied change MW"),("residual_mw","Residual MW"),("source","Measurement source")])
    loc=data.get("localization",{})
    localization_html=f'<p>Execution: {h(loc.get("status","not_requested"))}. {h(loc.get("reason",loc.get("interpretation","")))}</p>'
    if loc.get("runs"):
        localization_html+=table(loc["runs"],[("scope","Trial"),("included","Included IGMs"),("excluded","Omitted IGMs"),("status","Execution"),("baseline_converged","AC converged"),("directory","Evidence folder")])
        localization_html+=f'<p>Budget: {h(loc["budget"])}. Requested subsets not tested: {h(loc["untested"])}.</p>'
    top,seen = [],set()
    for item in findings:
        key = (item["scope"],item["code"])
        if item["severity"]=="ERROR" and key not in seen:
            top.append(item);seen.add(key)
        if len(top)==5: break
    first = "".join(f'<li><strong>{h(i["scope"])}</strong> — {h(i["message"])}</li>' for i in top)
    first = f"<ol>{first}</ol>" if first else "<p>No error-level findings in completed checks. Review coverage and warnings before drawing conclusions.</p>"
    text = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>CGMES diagnosis · {h(data['run_id'])}</title><style>
    :root{{color-scheme:light;--ink:#173044;--muted:#596d7b;--line:#dce5ea;--blue:#075985}}*{{box-sizing:border-box}}
    body{{margin:0;background:#f3f6f8;color:var(--ink);font:15px/1.55 system-ui,sans-serif}}header{{background:#102d40;color:white;padding:34px max(24px,calc((100vw - 1160px)/2))}}
    h1{{font-size:32px;letter-spacing:-1px;margin:4px 0}}header p{{color:#c9e0e8;margin:8px 0}}main{{max-width:1208px;margin:24px auto;padding:0 24px}}
    .eyebrow{{text-transform:uppercase;letter-spacing:2px;font-size:12px}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
    .metric,.panel,.finding{{background:white;border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}}.metric b{{font-size:32px;display:block}}.metric span{{color:var(--muted)}}
    h2{{font-size:20px;margin:0 0 14px}}h3{{font-size:17px;margin:10px 0}}a{{color:var(--blue)}}table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}
    .scroll{{overflow:auto}}.meta{{font-size:12px;color:var(--muted);overflow-wrap:anywhere}}.badge{{border-radius:4px;background:#edf2f5;padding:3px 7px;font-weight:bold}}.error{{border-left:5px solid #b42318}}.warn{{border-left:5px solid #b7791f}}.info{{border-left:5px solid #2585a2}}
    pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,monospace;background:#f5f8fa;padding:15px;border-radius:7px;max-height:450px;overflow:auto}}
    .ids{{font:12px ui-monospace,monospace;color:var(--muted);overflow-wrap:anywhere}}input,select{{padding:12px;border:1px solid #b9ccd6;border-radius:7px;font:inherit}}input{{flex:1;min-width:180px}}.filters{{display:flex;gap:10px;position:sticky;top:0;background:#f3f6f8;padding:12px 0;z-index:2;flex-wrap:wrap}}summary{{cursor:pointer;color:var(--blue)}}footer{{color:var(--muted);padding:25px 0;font-size:13px}}[hidden]{{display:none!important}}
    </style></head><body><header><div class="eyebrow">RCC engineering • offline diagnosis</div><h1>IGM → CGM diagnostic report</h1>
    <p>{h(data['run_id'])} · {h(data['created_utc'])}</p><p>Run status: <strong>{h(data['status'])}</strong> · Mode: {h(data['mode'])}</p></header><main>
    <div class="cards">{cards}</div><section class="panel"><h2>Start here</h2>{first}<p>Errors are observed conditions or failed checks. A <strong>hypothesis</strong> suggests what to investigate; it is not a proven root cause.</p></section>
    <section class="panel"><h2>IGM and combined-network replay</h2><div class="scroll"><table><thead><tr><th>Scope</th><th>Execution</th><th>Baseline AC result</th><th>Evidence</th></tr></thead><tbody>{''.join(scopes)}</tbody></table></div>
    <p>{h(data.get('assembly_interpretation',''))}</p>{diff}</section>
    <section class="panel"><h2>Solver experiments</h2>{trial_html}<p>Each experiment starts from the imported state. An AC improvement indicates sensitivity. A converged DC calculation does not establish AC feasibility.</p></section>
    <section class="panel"><h2>Area net positions</h2>{position_html}<p>Area definitions and boundary orientation must be complete. Missing areas or unsolved flows mean unavailable evidence. Country schedule CSVs are partial injection totals, not measured net positions.</p></section>
    <section class="panel"><h2>Recorded alignment stages</h2>{alignment_html}<p>These optional measurements are supplied by the operator. Dr.CGM calculates deltas; it does not apply GLSK scaling or claim to reproduce the production alignment.</p></section>
    <section class="panel"><h2>Bounded IGM subset replay</h2>{localization_html}</section>
    <section class="panel"><h2>Downloads and scope</h2><p><a href="report.json">Structured report</a> · <a href="findings.csv">Findings CSV</a> · <a href="manifest-resolved.json">Run configuration</a> · <a href="inventory.json">Input fingerprints and headers</a></p>
    <p>This diagnostic subset is not an ENTSO-E CGMES/SHACL conformance certificate. No scaling, remedial action or source repair is applied. Combined import replays the supplied snapshot; it does not reproduce an internal RCC pipeline's custom preprocessing.</p>
    <p>Examples are capped per rule and scope; counters retain all detected occurrences. Engine tables and source-index.sqlite retain further evidence. Reports contain model identifiers and should stay in the same approved environment as the inputs.</p></section>
    <h2>Findings</h2><div class="filters"><input id="search" aria-label="Search findings" placeholder="Search ID, TSO, source file, rule or equipment…"><select id="severity" aria-label="Severity"><option value="">All severities</option><option>ERROR</option><option>WARN</option><option>INFO</option></select></div>
    <p id="shown"></p>{''.join(rows)}<footer>Dr.CGM {h(data['tool_version'])} · Results reflect completed checks and the recorded PyPowSyBl configuration. Input files are unchanged.</footer></main>
    <script>const search=document.getElementById('search'),severity=document.getElementById('severity'),items=[...document.querySelectorAll('.finding')];function filter(){{let n=0;for(const el of items){{el.hidden=!(el.textContent.toLowerCase().includes(search.value.toLowerCase())&&(!severity.value||el.dataset.severity===severity.value));if(!el.hidden)n++;}}document.getElementById('shown').textContent=n+' of '+items.length+' saved examples shown';}}search.addEventListener('input',filter);severity.addEventListener('change',filter);filter();</script></body></html>'''
    (out / "report.html").write_text(text, encoding="utf-8")
