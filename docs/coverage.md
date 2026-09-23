# Coverage audit against the shared diagnosis discussion

This audit compares the original v0.1 script with the engineering checks proposed in the shared discussion reviewed on 23 September 2026. It paraphrases the requirements; the conversation itself is not redistributed. "Implemented" means a defined executable check exists, not that every possible form of the problem is covered or that causation is proven.

| Proposed capability | Original v0.1 | Dr.CGM v0.2 | Evidence and limits |
|---|---|---|---|
| Inventory, dependencies, profile selection, timestamps and source provenance | Implemented | Expanded | FullModel plus selected DCAT metadata; source index, input hashes and exact ID traces |
| Referential integrity and conflicting model data | Selected checks | Expanded | Adds selected wrong-type references, same-file repeated descriptions and reused mRIDs; not full RDFS/SHACL |
| Per-IGM versus combined CGM replay | Implemented | Expanded | Adds mutually exclusive assembled `cgm` mode to avoid mixing original and RCC-updated states |
| Topology islands, isolated buses, disconnected injections | Partial | Expanded | Component inventory, single-bus warning, disconnected generators/loads/batteries; expected topology still requires operator knowledge |
| Dangling/boundary pairing and schedule disagreement | Implemented | Retained | Expected internal keys must be supplied; external unpaired boundaries can be legitimate |
| P/Q balance per island | Partial | Partial | Partial scheduled P/Q totals and native validation/solver evidence; no claim of a full injection accounting identity |
| Country/control-area net position | Missing | Implemented with coverage conditions | Country schedules clearly labeled partial; Area interchange requires finite native boundary flows and valid area definitions |
| Original → target → aligned net-position delta | Missing | Implemented for supplied observations | Computes deltas with provenance; does not calculate production alignment or GLSK |
| Slack distribution eligibility and margin | Partial | Expanded, still partial | Generator P-limit margins, native distribution events, balance-strategy trials; no full participation/ramp/reserve eligibility assessment |
| Generator P/Q limits, local/remote voltage targets and Q availability | Partial | Expanded | Fixed-Q versus capability, solved-Q margin and voltage plausibility; no complete dynamic capability verification |
| Transformer taps, winding ratings, impedance and base voltages | Partial | Expanded | 2/3-winding rated-U checks, finite 3-winding leg impedance, tap range and near-zero 2-winding impedance; no full star-equivalent plausibility proof |
| HVDC/VSC and operational limits | Inventory only | Selected checks | HVDC target P/max P, VSC inverted Q limits, nonpositive loading limits; full controls, seasonal/emergency limit selection deferred |
| Native import and LF report tree | Implemented | Expanded | Complete JSON/text plus extracted balancing/control/outer-loop events |
| Final mismatch buses and adjacent equipment | Implemented | Retained | Final native NR residual evidence with exact source matches where available; residual location is not necessarily defective equipment |
| AC baseline / AC with DC initialization | Implemented | Retained | Independent variants from the imported state |
| Previous-values and full-voltage initialization | Missing | Added | Finite imported-state gate and OpenLoadFlow provider override |
| Reactive limits, transformer, shunt and phase controls off | Implemented | Retained | One-setting sensitivities |
| Distributed versus single slack | Implemented | Retained | Baseline-equivalent trials skipped |
| Alternative balance strategy, multiple slack buses, voltage-target handling | Missing | Added | Generation-P/load distribution, maxSlackBusCount and fixVoltageTargets |
| Separate DC load flow | Missing | Added | Explicit DC mode, distinguished from AC initialization; DC success does not certify AC feasibility |
| Individual control isolation | Missing | Added | Up to ten selected generator/tap/shunt controls; missing/ambiguous IDs visible |
| Problem-region localization | Missing | Added, bounded | Leave-one-IGM-out or explicit subsets, 1–20-run budget; not minimal failure proof or monotonic binary search |
| Compare failed/working models and configuration | Source statements | Expanded | CIM semantic diff plus manifest engine/diagnostic-setting differences |
| Synthetic fault injection and regression checks | Small suite | Expanded | Actual CGMES3 import and solver tests, stressed load case, broken references/ratings/timestamps, selected-control and subset tests |
| JSON for an LLM or internal triage application | Implemented | Expanded | Structured evidence and actions; no automatic LLM upload or causal claims |
| Reproduce complete IGM alignment, merge, GLSK scaling and preprocessing | Missing | Deferred | Requires the RCC's exact business rules, stage data and engine configuration |
| Full CGMES/NCP conformance and official rule catalogue | Missing | Deferred | Use the matching ENTSO-E profiles/validation tooling alongside Dr.CGM |
| Exhaustive control interaction search or automatic repair | Missing | Deferred | Single-setting trials cannot establish multi-control causation; no source or operational changes are made |

## How to read the results

A failed import prevents electrical diagnosis in that scope. A failed auxiliary check or experiment is separately visible even when the main worker completes. A bounded search can finish its budget with subsets still untested. These outcomes must not be read as a clean bill of health.

Full reference models are valuable but do not replace controlled fault injection with known expected findings. The bundled two-area example is two independent islands, not a proof of a production cross-border merge. The ReliCapGrid exercise documented separately supplies an additional real repository packaging/import regression, with observed limitations.
