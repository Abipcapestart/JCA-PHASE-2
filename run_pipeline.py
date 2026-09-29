"""CLI entrypoint - run the full Phase 2 pipeline end-to-end against one or
more saved jca_phase1 run results (the exact dict
jca_phase1.orchestrator.run_phase1() returns, i.e. Phase1Output.to_dict()).

jca_phase1 processes exactly one population per run - pass --phase1-output
once per population (e.g. a licensed run and a separate intended-to-treat
run for the same product) to consolidate them together.

Usage:
    python run_pipeline.py --phase1-output path/to/phase1_run.json
    python run_pipeline.py --phase1-output licensed.json --phase1-output itt.json
"""

from __future__ import annotations

import argparse
import json
import os
import uuid

from adapter import adapt_orchestrator_result
from p2_config import RESULTS_DIR
from docx_export import save_document
from excel_export import save_workbook
from graph import run_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run JCA Phase 2 - PICOS Set Consolidation end-to-end.")
    parser.add_argument("--phase1-output", required=True, action="append",
                         help="Path to a saved jca_phase1 run_phase1() result JSON (Phase1Output.to_dict()). "
                              "Repeat once per population (e.g. licensed + intended-to-treat).")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    phase1_outputs = []
    for path in args.phase1_output:
        with open(path, encoding="utf-8") as f:
            phase1_outputs.append(json.load(f))

    run_id = args.run_id or str(uuid.uuid4())[:8]
    adaptation = adapt_orchestrator_result(
        phase1_outputs, run_id=run_id, source_ref=", ".join(args.phase1_output))
    for w in adaptation.warnings:
        print(f"[adapter warning] {w}")

    result = run_pipeline(adaptation.phase2_input)

    result_path = os.path.join(RESULTS_DIR, f"{run_id}.json")
    with open(result_path, "w", encoding="utf-8") as f:
        f.write(result.model_dump_json(indent=2))

    excel_path = os.path.join(RESULTS_DIR, f"{run_id}.xlsx")
    save_workbook(result, adaptation.phase2_input, excel_path)

    docx_path = os.path.join(RESULTS_DIR, f"{run_id}.docx")
    save_document(result, adaptation.phase2_input, docx_path)

    print(f"status={result.overall_status}")
    print(f"populations={len(result.populations)}")
    print(f"pico_sets={sum(len(p.assembly.pico_sets) for p in result.populations)}")
    print(f"logs={[f'{entry.agent_id}/{entry.error_type}: {entry.message}' for entry in result.logs]}")
    print(f"result_json={result_path}")
    print(f"excel={excel_path}")
    print(f"word_doc={docx_path}")


if __name__ == "__main__":
    main()
