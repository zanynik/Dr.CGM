"""Additional physical sanity checks. Heuristic thresholds are not operating rules."""
import math
from .common import clean, write_csv


def finite(value):
    try: return math.isfinite(float(value))
    except (TypeError, ValueError): return False


def inspect_extended(n, tables, scope, out, findings, config):
    def emit(code, message, action, sid, evidence, severity="WARN", certainty="hypothesis"):
        findings.add(code,severity,scope,message,action,clean(evidence),[str(sid)],certainty)
    gens, buses, volts = tables["generators"], tables["buses"], tables["voltage_levels"]
    for kind in ("generators", "loads", "batteries"):
        frame=tables[kind]
        if "connected" not in frame: continue
        for sid,row in frame[~frame.connected].iterrows():
            emit("DISCONNECTED_INJECTION",f"A {kind} record is disconnected.","Confirm intended outage versus an unintended topology/disconnection change.",sid,{"kind":kind,**row.to_dict()},"INFO","observed")
    if not buses.empty:
        for (_, _), group in buses.groupby(["connected_component","synchronous_component"]):
            if len(group)==1:
                sid=group.index[0]
                emit("SINGLE_BUS_COMPONENT","A synchronous component contains only one bus.","Check whether this is an intended single-node equivalent, island or disconnected section.",sid,group.iloc[0].to_dict(),"INFO")
    headroom=[]
    for sid,row in gens.iterrows():
        if not row.get("connected",False): continue
        low,high,q=row.get("min_q_at_target_p"),row.get("max_q_at_target_p"),row.get("target_q")
        if not row.get("voltage_regulator_on",False) and all(finite(v) for v in (low,high,q)) and not low-1e-6<=q<=high+1e-6:
            emit("FIXED_Q_OUTSIDE_LIMITS","Non-regulating generator target Q is outside its capability at target P.","Check fixed-Q setpoint, capability curve and sign convention.",sid,row.to_dict())
        p,lo,hi=row.get("target_p"),row.get("min_p"),row.get("max_p")
        known=all(finite(v) and abs(v)<1e9 for v in (p,lo,hi))
        headroom.append({"id":str(sid),"bus_id":row.get("bus_id"),"target_p_mw":p,
                         "upward_margin_mw":max(0,hi-p) if known else None,
                         "downward_margin_mw":max(0,p-lo) if known else None,
                         "finite_limits":known,"interpretation":"P-limit margin only; not proof of slack participation, ramp feasibility or available reserve."})
    write_csv(out/"generator-active-margins.csv",headroom,["id","bus_id","target_p_mw","upward_margin_mw","downward_margin_mw","finite_limits","interpretation"])
    # Country schedules are never called actual net positions: losses and converters matter.
    schedules={}
    subs=tables.get("substations")
    if subs is not None and "substation_id" in volts and "country" in subs:
        for kind,field,sign in [("generators","target_p",1),("loads","p0",-1),("batteries","target_p",1)]:
            for sid,row in tables[kind].iterrows():
                if not row.get("connected",False):continue
                vl=row.get("voltage_level_id")
                sub=volts.loc[vl,"substation_id"] if vl in volts.index else None
                country=str(subs.loc[sub,"country"] or "UNKNOWN") if sub in subs.index else "UNKNOWN"
                item=schedules.setdefault(country,{"country":country,"generation_mw":0.,"load_mw":0.,"battery_mw":0.})
                key={"generators":"generation_mw","loads":"load_mw","batteries":"battery_mw"}[kind]
                value=row.get(field)
                if finite(value):item[key]+=float(value)
        for item in schedules.values():
            item["partial_schedule_mw"]=item["generation_mw"]+item["battery_mw"]-item["load_mw"]
            item["interpretation"]="Generation+battery-load; excludes losses and converter/boundary schedules. Not a measured net position."
    write_csv(out/"country-schedules.csv",list(schedules.values()),["country","generation_mw","load_mw","battery_mw","partial_schedule_mw","interpretation"])
    for kind in ("transformers2","transformers3"):
        for sid,row in tables[kind].iterrows():
            count=2 if kind=="transformers2" else 3
            for side in range(1,count+1):
                u=row.get(f"rated_u{side}");vl=row.get(f"voltage_level{side}_id")
                nominal=volts.loc[vl,"nominal_v"] if vl in volts.index else None
                if not finite(u) or u<=0:
                    emit("TRANSFORMER_RATING_INVALID","Transformer winding has no positive finite rated voltage.","Check winding data and conversion.",sid,{"side":side,"rated_u":u},"ERROR","observed")
                elif finite(nominal) and nominal>0:
                    ratio=u/nominal
                    if not config.get("rated_voltage_min_pu",0.75)<=ratio<=config.get("rated_voltage_max_pu",1.25):
                        emit("TRANSFORMER_VOLTAGE_BASE_MISMATCH","Transformer winding rating differs strongly from its voltage-level base.","Check winding side/base-voltage assignment; special transformer designs can be intentional.",sid,{"side":side,"rated_u":u,"nominal_v":nominal,"ratio":ratio})
                if kind=="transformers3" and not all(finite(row.get(f"{key}{side}")) for key in ("r","x")):
                    emit("THREE_WINDING_IMPEDANCE_INVALID","Three-winding transformer leg has nonfinite R/X.","Inspect transformer-end parameters and star-equivalent conversion.",sid,{"side":side,"r":row.get(f"r{side}"),"x":row.get(f"x{side}")},"ERROR","observed")
    for sid,row in tables["hvdc"].iterrows():
        if finite(row.get("target_p")) and finite(row.get("max_p")) and abs(row.target_p)>row.max_p+1e-6:
            emit("HVDC_P_LIMIT","HVDC active-power target exceeds its maximum.","Inspect the active-power schedule, direction and converter ratings.",sid,row.to_dict())
    for sid,row in tables["vsc"].iterrows():
        if finite(row.get("min_q")) and finite(row.get("max_q")) and row.min_q>row.max_q:
            emit("VSC_Q_LIMIT_ORDER","VSC reactive limits are inverted.","Check converter capability data.",sid,row.to_dict(),"ERROR","observed")
    limits=tables["limits"]
    if "value" in limits:
        for sid,row in limits.iterrows():
            if finite(row.value) and row.value<=0:
                emit("LOADING_LIMIT_NONPOSITIVE","An imported loading limit is nonpositive.","Check units, emergency/permanent limit definitions and selected limit set.",sid,row.to_dict())
    return {"country_schedules":list(schedules.values()),"generator_margins":headroom}


def area_positions(n, config, findings, scope, phase):
    areas=n.get_areas(all_attributes=True)
    boundaries=n.get_areas_boundaries(all_attributes=True)
    targets=config.get("area_net_position_targets_mw",{})
    result=[]
    for sid,row in areas.iterrows():
        target=targets.get(str(sid),row.get("interchange_target"))
        members=boundaries.loc[[sid]] if sid in boundaries.index else boundaries.iloc[0:0]
        coverage=len(members)>0 and members.p.map(finite).all()
        measured=row.get("interchange") if coverage else None
        delta=measured-target if finite(measured) and finite(target) else None
        item={"id":str(sid),"phase":phase,"target_mw":target,"interchange_mw":measured,
              "difference_mw":delta,"boundary_count":len(members),"complete_boundary_flows":bool(coverage),
              "interpretation":"Native area boundary interchange; validity depends on complete, correctly oriented area definitions."}
        result.append(clean(item))
        if delta is not None and abs(delta)>config.get("net_position_tolerance_mw",5.):
            findings.add("AREA_NET_POSITION_DEVIATION","WARN",scope,f"{phase}: area interchange differs from its target.","Verify area boundary completeness, target provenance, alignment stage and sign convention before changing injections.",item,[str(sid)],"hypothesis")
    for sid in set(targets)-set(map(str,areas.index)):
        findings.add("AREA_TARGET_UNMAPPED","WARN",scope,"Configured area target has no matching imported Area.","Use the exact imported Area ID or supply the missing area definitions.",{"area_id":sid},[sid])
    return result


def inspect_solution(n, scope, findings, config):
    volts=n.get_voltage_levels();buses=n.get_buses()
    for sid,row in buses.iterrows():
        vl=row.voltage_level_id;nom=volts.loc[vl,"nominal_v"] if vl in volts.index else None
        if not finite(row.v_mag) or not finite(nom) or nom<=0:continue
        pu=row.v_mag/nom
        if not config.get("solved_voltage_min_pu",0.8)<=pu<=config.get("solved_voltage_max_pu",1.2):
            findings.add("SOLVED_VOLTAGE_IMPLAUSIBLE","WARN",scope,"A converged solution contains voltage outside the configured plausibility band.","Check local operating limits, voltage controls and physical plausibility; convergence alone does not establish an acceptable state.",{"bus":sid,"voltage_kv":row.v_mag,"nominal_kv":nom,"pu":pu},[str(sid)],"hypothesis")
    gens=n.get_generators(all_attributes=True)
    for sid,row in gens.iterrows():
        if not row.connected:continue
        q=-row.q  # IIDM terminal power is positive into equipment; capability is injection convention.
        lo,hi=row.min_q_at_p,row.max_q_at_p
        if all(finite(v) and abs(v)<1e9 for v in (q,lo,hi)):
            margin=min(q-lo,hi-q)
            if margin<=config.get("reactive_margin_mvar",1.):
                findings.add("GENERATOR_Q_MARGIN_LOW","WARN",scope,"Solved generator Q is near or outside its reactive capability boundary.","Review PV/PQ switching and neighboring voltage controls; a binding Q limit can be normal.",{"generator":sid,"injection_q_mvar":q,"min_q":lo,"max_q":hi,"nearest_margin_mvar":margin},[str(sid)],"hypothesis")
