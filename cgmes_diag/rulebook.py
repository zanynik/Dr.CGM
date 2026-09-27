"""Human-readable explanations for selected diagnostic rules.

These describe how the check works, not a probabilistic confidence score.
Keep the descriptions aligned with the implementation when changing a rule.
"""

RULES = {
    "MULTIPLE_SYNCHRONOUS_COMPONENTS": {
        "method": "Group imported PowSyBl buses by connected and synchronous component; count the groups.",
        "implementation": "cgmes_diag/engine.py:inspect_network",
        "limits": "Multiple components can be expected, including HVDC-separated regions; this alone does not identify an error.",
    },
    "ISLAND_NO_OBVIOUS_VOLTAGE_SOURCE": {
        "method": "For each imported bus component containing load, look for connected regulating generators, VSCs or SVCs.",
        "implementation": "cgmes_diag/engine.py:inspect_network",
        "limits": "Boundary equivalents and other controls are not exhausted by this candidate check; review the solver component result.",
    },
    "EXPECTED_BOUNDARY_UNPAIRED": {
        "method": "Group imported boundary lines by pairing key and check paired status for keys explicitly listed in expected_internal_boundary_keys.",
        "implementation": "cgmes_diag/engine.py:inspect_network",
        "limits": "Requires an accurate operator supplied list of expected internal boundaries; no automatic inference of internal versus external borders.",
    },
    "BOUNDARY_P_DISAGREEMENT": {
        "method": "For two ends with the same pairing key, compare the sum of their scheduled p0 values to boundary_p_tolerance_mw.",
        "implementation": "cgmes_diag/engine.py:inspect_network",
        "limits": "Sign conventions, equivalent injections and losses may affect the interpretation; this is a schedule comparison, not a solved flow check.",
    },
    "AC_NOT_CONVERGED": {
        "method": "Run the baseline AC calculation on the imported variant and record each returned PowSyBl component status.",
        "implementation": "cgmes_diag/engine.py:worker",
        "limits": "The result depends on imported data, provider version and effective parameters; a failed solve alone does not identify its cause.",
    },
    "SOLVER_FINAL_MISMATCH": {
        "method": "Extract final equation mismatches from the native load-flow report and list equipment connected to the reported bus.",
        "implementation": "cgmes_diag/engine.py:final_mismatches",
        "limits": "Residual location is a lead for investigation, not proof that the connected equipment is defective.",
    },
    "EXPERIMENT_RESTORED_CONVERGENCE": {
        "method": "Clone the imported variant, change one configured solver/control setting, and compare component convergence to baseline AC.",
        "implementation": "cgmes_diag/engine.py:worker; cgmes_diag/experiments.py",
        "limits": "Sensitivity does not prove a root cause or make the changed setting operationally acceptable.",
    },
    "REPLAY_INCOMPLETE": {
        "method": "Record an incomplete worker result when import, execution or its time budget prevents a completed replay.",
        "implementation": "cgmes_diag/cli.py:run_engine",
        "limits": "No successful electrical diagnosis is implied for the missing stages; inspect stage coverage and local logs.",
    },
}


def explain(code):
    return RULES.get(code)
