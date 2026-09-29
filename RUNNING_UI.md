# Running the Phase 2 Streamlit UI

This is the Streamlit front-end for **Phase 2 — PICOS Set Consolidation**
([ui/app.py](ui/app.py)). It lets you upload one or more Phase 1 run outputs,
run the 4-agent consolidation pipeline, inspect/validate the results, tune
each agent's SME instruction text, and download the final Word/Excel/JSON
deliverables.

## 1. Prerequisites

- The virtual environment at [.venv/](.venv/) already has all dependencies
  installed (see [requirements.txt](requirements.txt): `streamlit`,
  `langgraph`, `pydantic`, `boto3`, `python-docx`, `openpyxl`, etc.). If you
  need to (re)create it:

  ```powershell
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
  ```

- AWS Bedrock credentials for the LLM calls (Agents 2–4) are read from
  `..\phase2\.env` (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_REGION`, `BEDROCK_MODEL_ID`) via `phase2/config.py`'s `load_dotenv()`.
  That file already exists in this workspace — nothing to configure unless
  credentials rotate.

  > If you edit that `.env` file, restart the Streamlit process afterwards —
  > env vars are only read once at import time, a re-run of the page in the
  > browser does **not** pick up changes.

## 2. Required: point Python at the `jca_phase1` package

[p2_config.py](p2_config.py) expects the `jca_phase1` package to be
importable as `jca_phase1.config`, and looks for it under
`phase2_pico_consolidation\jca_phase1\`. That folder **does not exist** in
this checkout — the real package lives one level up, at
`..\jca_phase1\jca_phase1\`. Without pointing Python at it, the app fails
immediately with `ModuleNotFoundError: No module named 'jca_phase1'`.

Fix it by setting `PYTHONPATH` to the real package root (`..\jca_phase1`)
before launching — see the run commands below, which already include this.

(A one-time alternative is to create a directory junction so the expected
path resolves on its own:
`cmd /c mklink /J jca_phase1 "..\jca_phase1"` — run from inside
`phase2_pico_consolidation\`. If you do this, you can drop `PYTHONPATH` from
the commands below.)

## 3. Run it

From the `phase2_pico_consolidation` directory:

**PowerShell:**
```powershell
$env:PYTHONPATH = "..\jca_phase1"
.venv\Scripts\streamlit run ui\app.py
```

**Git Bash:**
```bash
PYTHONPATH="../jca_phase1" .venv/Scripts/streamlit run ui/app.py
```

Streamlit prints a local URL (default `http://localhost:8501`) — open it in
your browser. Stop the server with `Ctrl+C` in the terminal.

## 4. Using the app

The app has two views, switched via the radio button at the top:

### 📥 Run Pipeline
1. **Upload** one or more Phase 1 run results. Each `jca_phase1` run covers
   exactly one population, so upload one file per population (e.g. licensed
   + intended-to-treat) to consolidate them together. Accepted formats:
   - A bare `Phase1Output.to_dict()` JSON (what
     `jca_phase1.orchestrator.run_phase1()` returns) — **preferred**.
   - A `scripts/run_gt_eval.py`-style evaluation JSON
     (`{drug, status, output: <Phase1Output>, ...}`).
   - A `JCA_Phase1_RUN-*.xlsx` UI export — a reporting view only; you'll be
     prompted for the intervention name and any missing
     `comparator_scenario` values it doesn't carry.
2. If prompted, fill in any missing `comparator_scenario` values — only set
   these if you actually know them from the source data; otherwise leave
   unset (that comparator's per-state rows are safely skipped with a
   warning).
3. Click **"Run Phase 2 Consolidation"**. This runs the 4 agents in
   sequence (context locking → PICO consolidation → set assembly →
   validation) via Bedrock.
4. Review the run details, per-population PICO sets, validation status, and
   error log.
5. Download the results as a **Word document** (final deliverable), an
   **Excel workbook** (full traceability), or **raw JSON**.

### 🛠️ Prompt Tuning (SME instructions)
Edit, version, activate, restore, and diff each agent's system-prompt
instruction text ([prompts/registry.py](prompts/registry.py)). The
output-format contract ([prompts/output_schemas.py](prompts/output_schemas.py))
is shown read-only for context — it's fixed and engineering-controlled, and
gets appended automatically at call time.

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: No module named 'jca_phase1'` | `PYTHONPATH` not set — see step 2. |
| `ModuleNotFoundError: No module named 'extraction'` or Bedrock/config errors | `PYTHONPATH`/cwd issue reaching `..\phase2\` — run Streamlit from inside `phase2_pico_consolidation\` as shown above, don't `cd` elsewhere first. |
| Bedrock call fails / empty response | Check `..\phase2\.env` credentials and `BEDROCK_MODEL_ID`; then restart Streamlit (env vars load once at startup). |
| Changed a `.env` value but behavior didn't change | Restart the Streamlit process — it doesn't hot-reload env vars. |
| Want a different results folder | Set `PHASE2_RESULTS_DIR` before launch (defaults to [results/](results/)). |
| Want a different prompt-version store | Set `PHASE2_PROMPT_STORE_DIR` before launch (defaults to [prompts/_store/](prompts/_store/)). |
