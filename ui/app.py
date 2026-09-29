"""Streamlit UI for JCA Phase 2 - PICOS Set Consolidation (task Section 24).

Single-page app: upload a Phase 1 run (xlsx or JSON), run the Phase 2
pipeline, inspect results, and tune each agent's SME instruction text - both
sections render together on the same page rather than a separate sidebar
page.

Run with: `streamlit run ui/app.py` from the phase2_pico_consolidation
directory (or set PYTHONPATH to it).
"""

import io
import json
import os
import sys
import uuid

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_DIR = os.path.dirname(_THIS_DIR)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

import streamlit as st  # noqa: E402

from adapter import Phase1OutputAdapterError, adapt_orchestrator_result, unwrap_phase1_run  # noqa: E402
from docx_export import build_document, build_table_rows, match_population_input  # noqa: E402
from excel_export import build_workbook  # noqa: E402
from graph import run_pipeline  # noqa: E402
from prompts.output_schemas import OUTPUT_SCHEMA_BY_AGENT  # noqa: E402
from prompts.prompt_builder import build_system_prompt  # noqa: E402
from prompts.registry import AGENT_IDS, get_registry  # noqa: E402
from xlsx_adapter import XlsxRunAdapterError, load_phase1_xlsx  # noqa: E402

st.set_page_config(page_title="JCA Phase 2 - PICOS Set Consolidation", layout="wide")
st.title("JCA Phase 2 — PICOS Set Consolidation")
st.caption(
    "Consolidates Phase 1's confirmed, per-Member-State comparator requirements into the "
    "smallest defensible set of PICO sets, following the SME's 4-agent methodology and the "
    "HTA-JCA scoping guidance's Section 3.2 consolidation process."
)

AGENT_LABELS = {
    "context_locking": "Agent 1 — Context / Methodology Locking",
    "pico_consolidation": "Agent 2 — PICO Consolidation",
    "set_assembly": "Agent 3 — Set Assembly",
    "validation": "Agent 4 — Validation",
}


def _evidence_rows_for_set(population_input, pico_set):
    """Per-(comparator, Member State) evidence backing one PICO set - the
    same data excel_export.py's Sheet 7 already computes, surfaced here too
    so full source/evidence traceability isn't Excel-only."""
    if population_input is None:
        return []
    comparator_by_name = {c.generic_name: c for c in population_input.comparators}
    rows = []
    for ref in (pico_set.source_comparator_refs or [pico_set.comparator]):
        comp = comparator_by_name.get(ref)
        if comp is None:
            continue
        for req in comp.per_member_state:
            rows.append({
                "Comparator": comp.generic_name, "Member State": req.member_state, "Tier": req.tier,
                "Source Reference": req.source_reference, "Evidence Quote": req.evidence_quote,
            })
    return rows


def _dedupe_columns(names):
    """Guards the preview table's dict-keyed columns against a (rare) case
    where two PICO sets in the same population share a comparator name -
    dict keys must be unique or the second column would silently overwrite
    the first. Not needed by the Word document (column-indexed, not
    dict-keyed) - preview-only."""
    seen = {}
    deduped = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        deduped.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return deduped

# =============================================================================
# SECTION 1 — Run Pipeline: upload Phase 1 output, run Phase 2, inspect, download
# =============================================================================
st.header("📥 Run Pipeline")
uploaded_files = st.file_uploader(
    "1. Upload one or more Phase 1 run results. jca_phase1 processes exactly one population "
    "per run - upload one file per population (e.g. licensed + intended-to-treat) to "
    "consolidate them together. Two JSON shapes are accepted directly: a bare "
    "`Phase1Output.to_dict()` (what `jca_phase1.orchestrator.run_phase1()` returns), or a "
    "`scripts/run_gt_eval.py`-style evaluation file such as `Lurbinectedin.json` "
    "(`{drug, status, output: <Phase1Output>, ...}`). A `JCA_Phase1_RUN-*.xlsx` UI export is "
    "also accepted, but is a REPORTING view, not the full output - it has no intervention name "
    "(you'll be asked for it below) and no `comparator_scenario` column, so you'll be asked to "
    "fill that in manually per comparator below before running. Prefer the JSON when you have it.",
    type=["json", "xlsx"], accept_multiple_files=True,
)

if uploaded_files:
    phase1_outputs = []
    load_errors = []
    xlsx_product_names = {}

    xlsx_files = [f for f in uploaded_files if f.name.lower().endswith(".xlsx")]
    if xlsx_files:
        st.warning(
            "xlsx export(s) uploaded - these have no `comparator_scenario` column, so every "
            "per-Member-State row will be skipped unless you fill it in below (see step 2). "
            "Use the original JSON instead when you have it.")
        for f in xlsx_files:
            xlsx_product_names[f.name] = st.text_input(
                f"Intervention/product name for {f.name!r} (not present in the xlsx - required)",
                key=f"product_name_{f.name}",
            )

    for f in uploaded_files:
        if f.name.lower().endswith(".xlsx"):
            product_name = xlsx_product_names.get(f.name, "")
            if not product_name:
                continue  # wait for the user to fill in the product name above
            try:
                phase1_outputs.append(load_phase1_xlsx(f, product_name=product_name))
            except XlsxRunAdapterError as exc:
                load_errors.append(f"{f.name}: {exc}")
        else:
            try:
                phase1_outputs.append(json.load(f))
            except json.JSONDecodeError as exc:
                load_errors.append(f"{f.name}: could not parse as JSON: {exc}")

    for e in load_errors:
        st.error(e)

    _SCENARIO_OPTIONS = [
        ("", "— leave unset (this comparator's per-state rows are skipped with a warning) —"),
        ("unique", "Unique — single required comparator"),
        ("each_required", "Each required — every listed comparator must be included"),
        ("at_least_one", "At least one — strongest is selected, others noted as droppable"),
        ("individualised", "Individualised — patient-specific, no single HTA-wide selection"),
    ]
    _SCENARIO_LABELS = dict(_SCENARIO_OPTIONS)

    scenario_gaps = []
    for idx, run in enumerate(phase1_outputs):
        try:
            output = unwrap_phase1_run(run)
        except Phase1OutputAdapterError:
            continue
        label = uploaded_files[idx].name if idx < len(uploaded_files) else f"run #{idx + 1}"
        for comp in output.get("comparators", []) or []:
            if not comp.get("comparator_scenario"):
                scenario_gaps.append((label, comp))

    if scenario_gaps:
        with st.expander(
            f"2. Fill in missing comparator_scenario ({len(scenario_gaps)} comparator(s) affected) "
            "— required for their per-Member-State data to feed Agent 2's methodology",
            expanded=True,
        ):
            st.caption(
                "This field isn't in the xlsx export and is never guessed. Only pick a value here if "
                "you actually know it from the underlying sources/rationale shown below - leave any "
                "entry unset to keep the safe default (that comparator's per-state rows stay skipped, "
                "with a warning, exactly as before)."
            )
            for i, (label, comp) in enumerate(scenario_gaps):
                st.markdown(f"**{comp.get('generic_name', '?')}** _(from {label})_")
                if comp.get("rationale"):
                    st.caption(comp["rationale"])
                choice = st.selectbox(
                    "comparator_scenario", options=[v for v, _ in _SCENARIO_OPTIONS],
                    format_func=lambda v: _SCENARIO_LABELS[v],
                    key=f"scenario_{label}_{comp.get('generic_name', '')}_{i}",
                )
                comp["comparator_scenario"] = choice

    preview_rows = []
    for run in phase1_outputs:
        row = {"drug": run.get("drug", "")}
        try:
            output = unwrap_phase1_run(run)
            row["harness_status"] = run.get("status", "n/a")
            row["input_validation_status"] = (output.get("input_validation") or {}).get("overall_status")
            row["population_id"] = ((output.get("populations") or [{}])[0]).get("population_id")
            row["comparator_count"] = len(output.get("comparators", []))
            row["outcome_count"] = sum(
                len(v) for k, v in (output.get("outcomes") or {}).items() if k != "catalog_coverage")
        except Phase1OutputAdapterError as exc:
            row["harness_status"] = run.get("status", "n/a")
            row["error"] = str(exc)
        preview_rows.append(row)
    if preview_rows:
        st.write("Preview:", preview_rows)

    if phase1_outputs and st.button("3. Run Phase 2 Consolidation", type="primary"):
        run_id = str(uuid.uuid4())[:8]
        progress = st.status("Running Phase 2 pipeline...", expanded=True)
        try:
            progress.write("Adapting Phase 1 output (Contract Adapter)...")
            source_ref = ", ".join(f.name for f in uploaded_files)
            adaptation = adapt_orchestrator_result(phase1_outputs, run_id=run_id, source_ref=source_ref)
            for w in adaptation.warnings:
                progress.write(f"⚠️ Adapter warning: {w}")

            progress.write("Agent 1 — Context/Methodology Locking...")
            progress.write("Agent 2 — PICO Consolidation (per population)...")
            progress.write("Agent 3 — Set Assembly (deterministic + rationale)...")
            progress.write("Agent 4 — Validation (deterministic + semantic)...")
            result = run_pipeline(adaptation.phase2_input)
            progress.update(label="Pipeline complete.", state="complete")

            st.session_state["phase2_result"] = result
            st.session_state["phase2_input"] = adaptation.phase2_input
        except Phase1OutputAdapterError as exc:
            progress.update(label="Adapter failed.", state="error")
            st.error(f"Phase 1 -> Phase 2 adaptation failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            progress.update(label="Pipeline failed.", state="error")
            st.error(f"Pipeline failed: {exc}")

if "phase2_result" in st.session_state:
    result = st.session_state["phase2_result"]
    phase2_input = st.session_state["phase2_input"]

    st.subheader("3. Run Details")
    r1, r2, r3 = st.columns(3)
    r1.metric("Run ID", result.run_id)
    r2.metric("Model", result.model or "unknown")
    r3.metric("Overall Status", result.overall_status)
    with st.expander("Prompt versions used this run (for reproducibility)"):
        st.dataframe(
            [{"Agent": AGENT_LABELS.get(a, a), "Active Version": v}
             for a, v in result.prompt_versions.items()],
            use_container_width=True, hide_index=True,
        )

    st.subheader("4. Agent 1 — Methodology")
    c1, c2, c3 = st.columns(3)
    c1.metric("Methodology Loaded", "Yes" if result.context_locking.methodology_loaded else "No")
    c2.metric("Guidance Version", result.context_locking.guidance_version or "unknown")
    c3.metric("Sections Covered", f"{len(result.context_locking.sections_covered)}/4")
    if result.context_locking.sections_covered:
        st.caption("Confirmed present: " + "; ".join(result.context_locking.sections_covered))

    if result.logs:
        st.error(
            f"⚠️ {len(result.logs)} agent failure(s) occurred during this run — see the Error Log below "
            "for exactly which agent, on what data, and why."
        )
        with st.expander(f"Error Log ({len(result.logs)} entries)", expanded=True):
            st.dataframe(
                [
                    {
                        "Timestamp": entry.timestamp,
                        "Level": entry.level,
                        "Agent": AGENT_LABELS.get(entry.agent_id, entry.agent_id),
                        "Population": entry.population or "—",
                        "PICO Set": entry.pico_set_id or "—",
                        "Cause (error type)": entry.error_type or "—",
                        "Message": entry.message,
                    }
                    for entry in sorted(result.logs, key=lambda e: e.timestamp)
                ],
                use_container_width=True, hide_index=True,
            )

    for pop in result.populations:
        population_input = match_population_input(phase2_input, pop)
        # `droppable_note` lives on Agent 2's SelectedComparator, not on
        # Agent 3's Phase2PicoSet (the SME's own Agent 3 output schema
        # has no such field either - it's read by Agent 3 as rationale
        # context, never copied forward) - looked up by comparator name
        # to show it against the right PICO set below.
        selected_by_name = {sel.generic_name: sel for sel in pop.consolidation.selected_comparators}
        logs_by_set_id = {}
        for entry in result.logs:
            if entry.pico_set_id:
                logs_by_set_id.setdefault(entry.pico_set_id, []).append(entry)
        st.markdown(f"## Population: {pop.population} ({pop.population_label})")
        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.metric("Selected comparators", len(pop.consolidation.selected_comparators))
        col_b.metric("PICO sets", len(pop.assembly.pico_sets))
        blocked_count = sum(1 for r in pop.validation.results if r.validation_result == "failed_blocked")
        col_c.metric("Blocked sets", blocked_count)
        ms_check = pop.validation.member_state_count_check
        col_d.metric("27-State Check", f"{'✅' if ms_check.matches else '❌ mismatch'} ({ms_check.total})")

        for s in pop.assembly.pico_sets:
            validation_row = next((r for r in pop.validation.results if r.set_id == s.pico_set_id), None)
            passed = validation_row is not None and validation_row.validation_result == "passed"
            label = f"{'✅' if passed else '🚫'} {s.pico_set_id} — {len(s.member_states)} Member State(s)"
            with st.expander(label):
                st.write("**Comparator (C):**", s.comparator)
                st.write("**Intervention (I):**", s.intervention)
                st.write("**Outcomes (O):**", ", ".join(s.outcomes))
                st.write("**Member States:**", ", ".join(s.member_states))
                st.write("**Source Tiers:**", s.tiers)
                st.write("**Combination type / selection basis:**", f"{s.combination_type} / {s.selection_basis}")
                st.write("**Source comparator refs:**", ", ".join(s.source_comparator_refs) or "—")
                selected = selected_by_name.get(s.comparator)
                if selected and selected.droppable_note:
                    st.write("**Droppable note:**", selected.droppable_note)
                st.write("**Rationale:**", s.rationale)
                if s.tie_break and s.tie_break.tie_existed:
                    st.info(f"Automatic tie-break applied: {s.tie_break.reason}")
                if validation_row:
                    if passed:
                        st.success("Validation: passed")
                    else:
                        st.error(f"Validation: BLOCKED — {validation_row.failure_reason}")

                for entry in logs_by_set_id.get(s.pico_set_id, []):
                    st.error(
                        f"🔴 Agent failure ({AGENT_LABELS.get(entry.agent_id, entry.agent_id)}, "
                        f"{entry.timestamp}) — **cause:** `{entry.error_type}`: {entry.message}"
                    )

                evidence_rows = _evidence_rows_for_set(population_input, s)
                if evidence_rows:
                    st.caption("Evidence / Sources (per Member State):")
                    st.dataframe(evidence_rows, use_container_width=True, hide_index=True)

        if pop.assembly.not_identified_states:
            st.write("**Not-identified Member States (no comparator/SoC found at scoping):**",
                      ", ".join(pop.assembly.not_identified_states))

    st.subheader("5. Final Deliverable Preview")
    st.caption(
        "One column per PICO set, ordered broadest-first - the exact layout of the downloadable "
        "Word document below: Member States / Source Tier / Intervention (constant) / Outcomes "
        "(constant, grouped efficacy / safety / QoL & symptoms) / Rationale (never blank)."
    )
    for pop in result.populations:
        population_input = match_population_input(phase2_input, pop)
        column_headers, rows = build_table_rows(pop, population_input)
        st.markdown(f"**{pop.population} ({pop.population_label})**")
        if not column_headers:
            st.caption("No PICO sets were produced for this population.")
            continue
        deduped_headers = _dedupe_columns(column_headers)
        preview_rows = [
            {"": label, **dict(zip(deduped_headers, values))}
            for label, values in rows
        ]
        st.dataframe(preview_rows, use_container_width=True, hide_index=True)

    st.subheader("6. Download")
    docx_document = build_document(result, phase2_input)
    docx_buf = io.BytesIO()
    docx_document.save(docx_buf)

    wb = build_workbook(result, phase2_input)
    xlsx_buf = io.BytesIO()
    wb.save(xlsx_buf)

    dl1, dl2, dl3 = st.columns(3)
    dl1.download_button(
        "📄 Word document (final deliverable)",
        data=docx_buf.getvalue(),
        file_name=f"phase2_pico_consolidation_{result.run_id}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    dl2.download_button(
        "📊 Excel workbook (full traceability)",
        data=xlsx_buf.getvalue(),
        file_name=f"phase2_pico_consolidation_{result.run_id}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    dl3.download_button(
        "🧾 Raw JSON result",
        data=result.model_dump_json(indent=2),
        file_name=f"phase2_pico_consolidation_{result.run_id}.json",
        mime="application/json",
    )


# =============================================================================
# SECTION 2 — Prompt Tuning: edit, version, and activate each agent's SME
# instruction text (prompts/registry.py). Only `instruction_text` is editable
# here - the output-format contract (prompts/output_schemas.py) is fixed and
# engineering-controlled, shown read-only for context, per
# prompts/prompt_builder.py's mandatory input/output-contract separation
# (instructions never carry the schema, the schema is appended at call time).
# =============================================================================
st.divider()
st.header("🛠️ Prompt Tuning (SME instructions)")
st.subheader("Prompt Instructions Editor")
st.caption(
    "Edit each agent's SME instruction text, save it as a new draft version, and activate "
    "the version Phase 2 should use on its next run. Only the instruction text is editable "
    "here - the output-format contract shown below it is fixed and engineering-controlled, "
    "and is appended automatically at call time (see prompts/prompt_builder.py)."
)

registry = get_registry()

agent_id = st.selectbox("Agent", AGENT_IDS, format_func=lambda a: AGENT_LABELS.get(a, a))

versions = registry.list_versions(agent_id)
active = registry.get_active(agent_id)
version_numbers = [v.version for v in versions]

st.markdown(f"#### {AGENT_LABELS.get(agent_id, agent_id)} — currently active: v{active.version}")

selected_version_num = st.selectbox(
    "Load version into editor", version_numbers, index=version_numbers.index(active.version),
    format_func=lambda v: f"v{v} — {next(x.status for x in versions if x.version == v)}",
)
selected_version = registry.get_version(agent_id, selected_version_num)

edited_text = st.text_area(
    "Instruction text (SME-editable)", value=selected_version.instruction_text,
    height=420, key=f"editor_{agent_id}_{selected_version_num}",
)

created_by = st.text_input("Your name (recorded as the author of any new draft)")

col1, col2, col3 = st.columns(3)
with col1:
    if st.button("Save as new draft", disabled=not created_by,
                 help="Creates a new, inactive version. Does not change what Phase 2 runs use."):
        new_v = registry.save_draft(agent_id, edited_text, created_by=created_by)
        st.success(f"Saved v{new_v.version} as a draft.")
        st.rerun()
with col2:
    if st.button("Save & activate immediately", disabled=not created_by, type="primary",
                 help="Creates a new version and makes it active. Phase 2's next run will use it."):
        new_v = registry.save_draft(agent_id, edited_text, created_by=created_by)
        registry.activate(agent_id, new_v.version)
        st.success(f"Saved and activated v{new_v.version}.")
        st.rerun()
with col3:
    if selected_version.status != "active":
        if st.button(f"Activate v{selected_version_num} as-is",
                     help="No new version is created - this exact version becomes active."):
            registry.activate(agent_id, selected_version_num)
            st.success(f"Activated v{selected_version_num}.")
            st.rerun()
    else:
        st.write(f"v{selected_version_num} is already active.")

st.divider()

st.subheader("Version history (audit trail)")
st.dataframe(
    [
        {"version": v.version, "status": v.status, "created_by": v.created_by,
         "created_at": v.created_at, "updated_at": v.updated_at}
        for v in sorted(versions, key=lambda v: v.version, reverse=True)
    ],
    use_container_width=True, hide_index=True,
)

with st.expander("Restore an older version (reactivates it - no new version is created)"):
    restore_choices = [v.version for v in versions if v.status != "active"]
    if restore_choices:
        restore_target = st.selectbox("Version to restore", restore_choices, key="restore_target")
        if st.button("Restore"):
            registry.restore(agent_id, restore_target)
            st.success(f"Restored v{restore_target} as active.")
            st.rerun()
    else:
        st.write("No other versions to restore.")

with st.expander("Compare two versions"):
    if len(version_numbers) >= 2:
        c1, c2 = st.columns(2)
        va = c1.selectbox("Version A", version_numbers, index=0, key="cmp_a")
        vb = c2.selectbox("Version B", version_numbers, index=len(version_numbers) - 1, key="cmp_b")
        if st.button("Show diff"):
            diff = registry.compare(agent_id, va, vb)
            st.code(diff or "(no differences)", language="diff")
    else:
        st.write("Need at least 2 versions to compare.")

st.divider()

st.subheader("Fixed output-format contract (read-only, engineering-controlled)")
st.code(OUTPUT_SCHEMA_BY_AGENT[agent_id], language="json")

st.subheader("Exact system prompt Phase 2 sends today (active version + fixed contract)")
st.code(build_system_prompt(agent_id), language="text")
