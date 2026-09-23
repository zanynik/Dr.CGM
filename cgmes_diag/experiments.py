"""Bounded, reproducible experiments; every trial starts from the imported variant."""
from copy import deepcopy


def merge_parameters(base, delta):
    result = deepcopy(base)
    for key, value in delta.items():
        if key == "provider_parameters": result[key] = {**result.get(key, {}), **value}
        else: result[key] = value
    return result


def plan(pp, config, has_voltage_state):
    def trial(name, delta=None, **extra):
        return {"name": name, "delta": delta or {}, "mode": "ac", **extra}
    trials = [trial("baseline_ac")]
    if config.get("experiments", True):
        trials += [
            trial("dc_initialization", {"voltage_init_mode": pp.loadflow.VoltageInitMode.DC_VALUES}),
            trial("previous_values", {"voltage_init_mode": pp.loadflow.VoltageInitMode.PREVIOUS_VALUES},
                  skip=None if has_voltage_state else "No complete finite positive imported bus voltage state."),
            trial("full_voltage_initialization", {"provider_parameters": {"voltageInitModeOverride": "FULL_VOLTAGE"}}),
            trial("reactive_limits_off", {"use_reactive_limits": False}),
            trial("transformer_voltage_control_off", {"transformer_voltage_control_on": False}),
            trial("shunt_voltage_control_off", {"shunt_compensator_voltage_control_on": False}),
            trial("phase_control_off", {"phase_shifter_regulation_on": False}),
            trial("single_slack", {"distributed_slack": False}),
            trial("generation_p_balance", {"balance_type": pp.loadflow.BalanceType.PROPORTIONAL_TO_GENERATION_P}),
            trial("load_balance", {"balance_type": pp.loadflow.BalanceType.PROPORTIONAL_TO_LOAD}),
            trial("multiple_slack_buses", {"provider_parameters": {"maxSlackBusCount": "3"}}),
            trial("fix_voltage_targets", {"provider_parameters": {"fixVoltageTargets": "true"}}),
            trial("dc_loadflow", mode="dc"),
        ]
    for i, control in enumerate(config.get("control_experiments", [])):
        trials.append(trial(f"control_{i+1:02d}", control=control))
    return trials


def disable_control(network, spec):
    mappings = {
        "generator": ("get_generators", "update_generators", "voltage_regulator_on"),
        "shunt": ("get_shunt_compensators", "update_shunt_compensators", "voltage_regulator_on"),
        "ratio_tap": ("get_ratio_tap_changers", "update_ratio_tap_changers", "regulating"),
        "phase_tap": ("get_phase_tap_changers", "update_phase_tap_changers", "regulating"),
    }
    getter, updater, field = mappings[spec["kind"]]
    sid = spec["element_id"]
    table = getattr(network, getter)()
    if sid not in table.index: raise ValueError(f"Control element not found: {sid}")
    selected = table.loc[sid]
    if getattr(selected, "ndim", 1) != 1: raise ValueError("Ambiguous multi-leg tap control; use an unambiguous equipment ID")
    before = bool(selected[field])
    if before: getattr(network, updater)(id=sid, **{field: False})
    return {"kind":spec["kind"],"element_id":sid,"field":field,"before":before,"after":False}


def report_events(report):
    """Retain native outer-loop, balancing and voltage-target messages with their values."""
    import json
    data = json.loads(report.to_json())
    events = []
    stack = [(data.get("reportRoot", {}), {})]
    while stack:
        node, parent = stack.pop()
        vals = {k:v.get("value") if isinstance(v,dict) else v for k,v in node.get("values",{}).items()}
        context = {**parent, **{k:v for k,v in vals.items() if k in ("networkNumCc","networkNumSc")}}
        key = node.get("messageKey", "")
        if any(term in key.lower() for term in ("outerloop", "distribution", "balance", "voltagetarget", "voltagecontrol", "pvt", "pqt", "slack")):
            events.append({"message_key":key,"component":context,"values":vals})
        stack.extend((child, context) for child in reversed(node.get("children", [])))
    return events
