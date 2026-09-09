# Supplementary: labeller robustness and ablations — `supp_robustness` — 27 tasks with a valid reference in this harness

| condition | auto-accept | abstain / incorrect block | unsafe / failed variants | false blocks / passed variants | synthetic kill | same gate verdicts as v1 (all variants) |
|---|---|---|---|---|---|---|
| DeltaSpec v1 (frozen contract, re-scored) | 27/27 (1.00) | 0 / 0 | 6/105 (0.06) | 1/57 (0.02) | 115/135 (0.85) | 27/27 |
| re-label: qwen3.5 (frozen labeller, determinism) | 27/27 (1.00) | 0 / 0 | 6/105 (0.06) | 1/57 (0.02) | 115/135 (0.85) | 27/27 |
| re-label: gemma3:27b | 27/27 (1.00) | 0 / 0 | 3/105 (0.03) | 4/57 (0.07) | 118/135 (0.87) | 22/27 |
| re-label: phi4 (14B) | 27/27 (1.00) | 0 / 0 | 1/105 (0.01) | 4/57 (0.07) | 120/135 (0.89) | 23/27 |
| ablation: no exact-count facts | 27/27 (1.00) | 0 / 0 | 41/105 (0.39) | 0/57 (0.00) | 114/135 (0.84) | 10/27 |
| ablation: no scope invariant | 27/27 (1.00) | 0 / 0 | 6/105 (0.06) | 1/57 (0.02) | 61/135 (0.45) | 27/27 |
| ablation: no prohibitions | 27/27 (1.00) | 0 / 0 | 6/105 (0.06) | 1/57 (0.02) | 115/135 (0.85) | 27/27 |
| ablation: every fact required (no LLM) | 27/27 (1.00) | 0 / 0 | 0/105 (0.00) | 4/57 (0.07) | 121/135 (0.90) | 24/27 |
| ablation: table effects only (no LLM) | 27/27 (1.00) | 0 / 0 | 41/105 (0.39) | 0/57 (0.00) | 107/135 (0.79) | 10/27 |