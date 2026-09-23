# Verification record

Local verification: 23 September 2026, Linux x86_64, Python 3.12.14, PyPowSyBl 1.16.1 and its packaged OpenLoadFlow provider. `tested-environment.txt` records the Python environment; it is an audit record, not a portable dependency lockfile.

## Automated regression suite

Run from the repository root:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Local result: **27 passed in 36.66 seconds**. The v0.2 suite exercises:

- Actual CGMES 3.0 export/import, converged AC, the 14-case experiment plan and independent DC mode, with source checksums unchanged.
- Actual stressed-network non-convergence, native final residual locations and source-linked adjacent equipment.
- Two IGM imports and all-component combined replay; bounded subset replay with one healthy and one stressed independent area, including the untested-budget count.
- Individual generator-control changes on a cloned variant, with the original control state preserved.
- Generator active-limit, fixed-Q capability and implausible-voltage-target faults.
- Native area boundary interchange after solving and unavailable measurements when boundary flows are nonfinite.
- Deep provider-parameter overlays, retained diagnostic reporting settings and rejection of string booleans.
- CGM/IGM stage exclusivity, alignment observations/deltas, target-miss findings and safe subset limits.
- ReliCapGrid packaging selection: updated SSH included, original SSH excluded, and missing required inputs rejected.
- RDF/UUID identity normalization, ordinary cross-profile ID reuse, enum URIs, missing/wrong-type references, reused mRIDs and scalar conflicts.
- FullModel and selected DCAT boundary metadata/dependencies; timestamp timezone equivalence and EQ publication-date exclusion.
- Missing/cyclic dependencies, malformed-XML rollback, entity rejection and archive path isolation.
- Nonfinite values, zero base voltage and invalid tap position.
- Semantic snapshot comparison, preflight outcomes, finding caps with full counters, and unsupported configuration rejection.
- Native tie-line removal and expected-internal versus external unpaired boundaries.
- Worker timeout coverage reported as incomplete.

Core import/solver tests use the real native engine. The missing-boundary-flow branch uses real network tables with deliberately nonfinite boundary P values; it is not a claim that every imported state lacks flows. Tests provide controlled regression evidence, not an estimate of real-world diagnosis precision or recall.

## ReliCapGrid integration exercise

A separate local exercise used the real 27-profile CGM package at commit `f09bb340795122b43d6499e913c121bb21458859`. It demonstrated source findings and import-setting sensitivity, then completed a 275-bus replay with three converged component results, one no-calculation result and five failed component results. Imported-state validation reported unsupported nonlinear-shunt handling. See [docs/relicapgrid.md](docs/relicapgrid.md) for exact assembly/settings and limitations.

This is not counted as a passing clean-reference load flow. No reference data is bundled; a local clone is required to reproduce it. The default CI suite does not clone ReliCapGrid or depend on its moving branches.

## Platform and report checks

GitHub Actions runs the same suite on `ubuntu-latest` and `windows-latest` with Python 3.12. Consult the Actions run for the exact published commit. Local execution here was Linux; Windows instructions should be interpreted together with the Windows CI outcome.

HTML generation, section content and local evidence links are checked from generated reports. A browser executable was unavailable locally, so no browser visual verification is claimed. Mermaid diagrams live in Markdown and render on GitHub; diagnostic HTML has no external scripts or resources.

## Not established

No confidential RCC/OPDE case, modified engine parity, full pan-European runtime or complete CGMES 2.4.15 replay has been verified. The bundled two-IGM synthetic case contains independent islands; native boundary behavior is tested separately. The reference exercise adds CGMES packaging coverage but does not establish a fully valid cross-border production merge or reproduce the RCC's GLSK/alignment rules.
