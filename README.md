# DeltaSpec: mining intent-labelled effect contracts for stateful release gating of tool-using agents

DeltaSpec turns one **known-good execution** of a natural-language task into an executable
**effect contract** that a stateful release gate can evaluate on any later agent version:

1. **Mine** (`src/deltaspec/mine.py`, deterministic): from the reference state transition, typed
   facts that hold by construction — which tables gained/lost/modified rows, exact counts,
   constant field values of new rows, foreign-key/list relations to the acting user's data,
   selectors and transitions of modified rows, prohibitions.
2. **Label** (`src/deltaspec/label.py`, one LLM call): a small local model labels each fact
   `required` / `side_effect` / `incidental` / `unsure` under a closed JSON grammar
   (`prompts/deltaspec_intent_labeler_v1.md`). The model authors nothing.
3. **Assemble** (`src/deltaspec/assemble.py`, deterministic): required facts become a contract
   (DSL v0.5, `schemas/effect_contract.schema.json`) with one scope invariant; abstain if no
   effect is required.
4. **Gate** (`src/effectgate/contracts/evaluator.py`, deterministic, fail-closed): evaluates a
   contract on a restored pre-state and a candidate post-state.
5. **Validate** (`src/deltaspec/mutants.py`, `scripts/appworld_worker.py::op_variants`):
   synthetic state mutants and generic *execution variants* of the reference (skip the k-th
   mutating call, duplicate the first, skip all, extra read, foreign task), each labelled by
   AppWorld's official evaluator.

The Python package name of the gate/library code is `effectgate` (the internal name of the
gate prototype); `deltaspec` is the method. Nothing here was modified after the frozen
evaluation.

## Repository structure

```
src/deltaspec/            miner, labeller interface + grammar, assembler, synthetic mutants
src/effectgate/           state view (contracts/fieldview.py), contract language and evaluator
                          (contracts/evaluator.py, schema.py, io.py), AppWorld adapter
                          (adapters/appworld_adapter.py), Ollama provider, statistics
                          (analysis/stats.py), run recorder; the direct-authoring baseline
                          compiler + linker (contracts/compiler.py, linker.py, validator.py)
scripts/                  experiment drivers and analyses (see "Reproducing")
prompts/                  frozen labeller prompt; frozen prompts of the direct-authoring baseline
schemas/                  contract JSON schema
configs/models.yaml       model presets
tests/                    offline tests for the DeltaSpec pipeline and the contract linker
results/                  machine-readable artifacts behind every reported number (see below)
docs/PROTOCOL_TIMELINE.md timestamped protocol record (freeze, selection after the freeze,
                          transport-retry rule, post-hoc harness condition) with the artifact
                          each timestamp comes from
LICENSE                   MIT
```

## Prerequisites

* Linux, Python ≥ 3.10, ~2 GB free disk for AppWorld data, no GPU required.
* [Ollama](https://ollama.com) reachable at `OLLAMA_BASE_URL` with the models you intend to use.
  Reported experiments used `qwen3.5:latest` (labeller, 6.6 GB) and, for the supplementary
  robustness study, `gemma3:27b` and `phi4:latest`; the direct-authoring baseline used
  `qwen3.5:latest`. All calls are local; no paid API is used.
* AppWorld (benchmark and official evaluator) — **not redistributed here**, see next section.

```bash
bash scripts/bootstrap.sh            # creates .venv, installs this package + dev deps
cp .env.example .env                 # set OLLAMA_BASE_URL (never assume localhost) and APPWORLD_ROOT
source .venv/bin/activate
python scripts/check_ollama.py       # endpoint + model tags
pytest -q tests/test_deltaspec.py tests/test_linker.py   # offline, seconds
```

Environment variables read at run time: `OLLAMA_BASE_URL`, `OLLAMA_TIMEOUT_S` (900 used),
`OLLAMA_THINK=false` (disables "thinking" for models that support it; used for `qwen3.5`),
`APPWORLD_ROOT`.

## AppWorld

DeltaSpec is evaluated on AppWorld (Trivedi et al., ACL 2024). Install it yourself with
"bash scripts/install_appworld.sh", which creates a **separate** virtual environment
(`.venv-appworld`; AppWorld pins pydantic 1.x) and downloads the data to `APPWORLD_ROOT`
**outside** this repository (the downloader deletes `<root>/data` without asking). The adapter
(`src/effectgate/adapters/appworld_adapter.py`) talks to `scripts/appworld_worker.py`, which runs
inside `.venv-appworld` and is the only code that imports `appworld`.

Two AppWorld facts the harness depends on: `AppWorld.save_state()/load_state()` restore the
world exactly (verified per task; `results/appworld/api_verification.json`,
`determinism.json`), and the official per-task evaluator is used **only** to label candidate
executions — never to construct contracts.

## Two frozen harness variants - one documented line

The paper reports two frozen systems that differ in exactly one line of
`scripts/appworld_worker.py` (the `AppWorld(...)` constructor in `_open()`):

| frozen record | constructor | reported results |
|---|---|---|
| `deltaspec_v1` (**primary**) | library default `raise_on_failure=True` | primary validation table, transport retry, direct-authoring baseline |
| `deltaspec_v1_rof` (**post-hoc**) | `raise_on_failure=False`, as in AppWorld's own verifier | post-hoc harness condition on the five previously unevaluable references; supplementary robustness/ablation run |

**The shipped `scripts/appworld_worker.py` is the post-hoc (`_rof`) version.** Byte-exact
copies of both versions and the unified diff between them are in `scripts/frozen/`
(`appworld_worker.deltaspec_v1.py`, `appworld_worker.deltaspec_v1_rof.py`,
`appworld_worker.v1_to_rof.patch`). Switch and verify either exact frozen system:

```bash
# primary system of the paper (raise_on_failure=True)
cp scripts/frozen/appworld_worker.deltaspec_v1.py scripts/appworld_worker.py
python scripts/freeze_system.py --verify results/frozen_systems/deltaspec_v1.json      # -> UNCHANGED: 25 files

# post-hoc harness-fixed system (raise_on_failure=False) — the shipped default
cp scripts/frozen/appworld_worker.deltaspec_v1_rof.py scripts/appworld_worker.py
python scripts/freeze_system.py --verify results/frozen_systems/deltaspec_v1_rof.json  # -> UNCHANGED: 25 files

# equivalently, apply the one-line patch to the primary version
patch scripts/appworld_worker.py scripts/frozen/appworld_worker.v1_to_rof.patch
```

Under the primary default, ground-truth programs that deliberately probe API error
responses abort inside the reference (five Tier-A tasks); under `False` they run to
completion. Both frozen states verify byte-for-byte against their records.

## Reproducing the reported evaluation

Task IDs of the validation set were selected **after** the system was frozen and are recorded
in `results/deltaspec_fresh/VALIDATION_SET_FROZEN.json` (Tier A: 11 scenario-fresh tasks;
Tier B: 16 task-fresh tasks; exclusion-file hashes included).

```bash
# 1. verify the frozen system you intend to reproduce (see "Two frozen harness variants")
python scripts/freeze_system.py --verify results/frozen_systems/deltaspec_v1_rof.json   # shipped default
#   for the primary table: copy scripts/frozen/appworld_worker.deltaspec_v1.py into place and verify deltaspec_v1.json

# 2. main run (variants + official labels + baselines), ~5 min per task, local Ollama
IDS=$(python -c "import json;print(','.join(json.load(open('results/deltaspec_fresh/VALIDATION_SET_FROZEN.json'))['task_ids']))")
OLLAMA_THINK=false python scripts/run_deltaspec.py --task-ids $IDS \
    --model qwen3.5:latest --label my_fresh --out results/my_fresh
python scripts/deltaspec_summary.py --dir results/my_fresh --condition my_fresh \
    --expected results/deltaspec_fresh/VALIDATION_SET_FROZEN.json

# 3. direct-authoring baseline (frozen prompt/config of the final authoring prototype)
OLLAMA_THINK=false python scripts/run_appworld_sanity.py --task-ids $IDS --model qwen3.5:latest \
    --prompt prompts/contract_compiler_appworld_v6_linked.md \
    --repair-prompt prompts/contract_repair_appworld_v5_relational.md \
    --label my_baseline --out results/my_baseline --num-ctx 16384 --field-aware --linker --vocab-grammar --repair-rounds 0
python scripts/eval_contracts_on_variants.py --contracts results/my_baseline/sanity_runs.jsonl \
    --condition my_baseline --task-ids <ids with a contract> --label my_oneshot \
    --out results/my_fresh/oneshot_on_variants.jsonl

# 4. labeller robustness + ablations on the frozen system (one variant execution per task)
OLLAMA_THINK=false python scripts/run_deltaspec_robustness.py \
    --task-ids-from results/deltaspec_fresh/VALIDATION_SET_FROZEN.json \
    --labellers qwen3.5:latest,gemma3:27b,phi4:latest --label my_supp --out results/my_supp
python scripts/deltaspec_robustness_summary.py --dir results/my_supp --label my_supp

# 5. per-scenario tables and paired statistics (reads results/deltaspec_fresh + baseline rows)
python scripts/deltaspec_stats.py --out results/my_stats
python results/deltaspec_fresh/tier_analysis.py deltaspec_fresh   # tier split of the recorded run
```

Selecting a *new* validation set with the same procedure: `scripts/select_deltaspec_validation_set.py`
(two tiers, seeded). Its exclusion list is derived from earlier result files that are not part
of this package; the exact excluded task ids and the SHA-256 of every exclusion file are recorded
inside `VALIDATION_SET_FROZEN.json`, so the recorded selection can be verified without them.

Runtime: ~205 s per task for the seven execution variants (restore + replay of a ~365k-row
world), 15–30 s per labelling call when the Ollama server is not shared, 180–900 s per
direct-authoring compilation. Labelling uses temperature 0, seed 42, `num_ctx` 8192,
`num_predict` 512, grammar-constrained decoding.

## Which artifacts back which reported result

| reported result | artifact |
|---|---|
| Primary frozen validation (21/22 auto-accept, 0 incorrect blocks, 76/80 rejected, 4/80 unsafe, 1/46 false blocks) | `results/deltaspec_fresh/runs.jsonl` (condition `deltaspec_fresh`), `summary_deltaspec_fresh.json`, `tier_analysis_primary.md` |
| Transport retry; post-hoc harness condition (`raise_on_failure=False`) | same file, conditions `deltaspec_fresh_retry_transport`, `deltaspec_fresh_rof`; `tier_analysis_primary_plus_retry.md`, `tier_analysis_all_posthoc.md` |
| Output-text and state-hash baselines on the same variants | per-row fields `baseline_output_equal`, `baseline_hash_equal`, `output_baseline`, `hash_baseline` in `runs.jsonl` |
| Direct-authoring baseline on the same 27 tasks | `results/appworld_linked_v061_freshB/sanity_runs.jsonl` (conditions `linked_v061_freshB`, `linked_v061_freshB_rof`); its contracts scored on the same variants: `results/deltaspec_fresh/oneshot_on_variants*.jsonl` |
| Paired statistics, per-scenario tables | `results/deltaspec_supp/stats/stats.{md,json}` (`scripts/deltaspec_stats.py`) |
| Labeller robustness and ablations | `results/deltaspec_supp/robustness_runs.jsonl`, `summary_supp_robustness.{md,json}` |
| Development data (7 diagnostic + 13 tasks; ledger table) | `results/deltaspec_dev/runs.jsonl` (`deltaspec_dev7`, `deltaspec_dev7_v2`, `deltaspec_dev13`), `summary_*.json`; direct-authoring prototype numbers: `results/frozen_baselines/*/FROZEN_MANIFEST.json`, `results/frozen_baselines/linked_dsl_v0.6.1/` |
| Worker equivalence after the harness change | `results/deltaspec_dev/worker_equivalence_*.json` |
| In-house dogfood demonstration (earlier prototype, not DeltaSpec) | `results/dogfood/gate_runs.jsonl`, `gate_report_*.json` |
| AppWorld API/restore/integrity checks | `results/appworld/*.json` |

Every row in `runs.jsonl` carries the instruction, mined facts, labels, assembled contract,
per-variant verdicts, the official evaluator output per variant, and timing. Per-call LLM
records (prompts, raw outputs, durations) are in the `provider_calls.jsonl` files; the exact
prompt text used by each condition is archived under `results/*/prompts/`.

## Frozen systems and hashes

| record | meaning |
|---|---|
| `results/frozen_systems/deltaspec_v1.json` | the system that produced the primary validation: 25 files with SHA-256, labeller model/config, combined hash `d8059a1ea51c58c1…` |
| `results/frozen_systems/deltaspec_v1_rof.json` | identical except the one-line worker change (`raise_on_failure=False`); used for the post-hoc harness condition and the robustness study; combined hash `33be9229cc99c6b9…` |
| `results/frozen_systems/linked_dsl_v0.6.1.json` | the direct-authoring baseline (final authoring prototype) |
| `results/frozen_baselines/*/FROZEN_MANIFEST.json` | hashes of result files of each frozen system |

`python scripts/freeze_system.py --verify <record>` reports which files differ from the record.
The protocol chronology (what was fixed before which run) is in `docs/PROTOCOL_TIMELINE.md`.

## Historical environment metadata in the frozen records

Result directories were locked read-only after each run and are copied here byte-for-byte so
that the hashes in the freeze records and manifests still verify. As a consequence, the
following files contain the **address of the private Ollama server and the AppWorld root
path of the machine the experiments ran on** (an `http://…:11434` URL on a private network
and a `/home/<user>/appworld-root` path): the run manifests (`results/*/manifest*.json`,
`results/*/sanity_manifest*.json`), the freeze records (`results/frozen_systems/*.json`,
`FROZEN_MANIFEST.json`), the per-call provider logs (`results/*/provider_calls.jsonl`), the
run rows that record a transport error message (`results/*/runs.jsonl`,
`sanity_runs.jsonl`), and `results/appworld/appworld_integrity.json`. These values are
historical metadata only: they are not read by any script, they are not credentials, and
reproduction uses your own `OLLAMA_BASE_URL` and `APPWORLD_ROOT` from `.env`. The
configuration template `.env.example` contains placeholders only.

## Tests

`tests/test_deltaspec.py` (mining, assembly, mutants, grammar, regression/tolerance on a fixture
world) and `tests/test_linker.py` (baseline linker) run offline in seconds. AppWorld-dependent
paths are exercised by the scripts above.

## License

MIT (see `LICENSE`). Dependencies are permissively licensed (Apache-2.0/BSD/MIT/PSF). AppWorld
(Apache-2.0) is not redistributed; install it from its own distribution as described above.

## Citation

Paper: *DeltaSpec: Mining Intent-Labelled Effect Contracts from Reference Executions for
Stateful Release Gating of Tool-Using Agents* (submitted to IAAI-27).
