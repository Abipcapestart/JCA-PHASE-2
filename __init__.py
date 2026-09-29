"""JCA Product Phase 2 - PICOS Set Consolidation.

Consumes Phase 1's actual FinalScopingOutput (phase1_scoping/schema.py) and
consolidates per-Member-State comparator requirements into the smallest
defensible set of PICO sets, following the SME's 4-agent methodology
(Context-Locking -> PICO Consolidation -> Set Assembly -> Validation) and the
HTA-JCA scoping guidance's Section 3.2 consolidation methodology.

See PHASE2_JCA_PICO_CONSOLIDATION/PHASE2_MINIMAL_CHANGE_IMPLEMENTATION_PLAN.md
for the full evidence-based architecture this package implements. Phase 1 is
NOT modified by this package - it is a pure downstream consumer of
phase1_scoping's real, existing output contract.
"""
