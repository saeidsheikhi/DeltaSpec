
# Condition set `primary` (deltaspec_fresh) — 27 rows, 126 paired variant cases

| scenario | tier | tasks | evaluable | auto-accept | unsafe / failed variants | false blocks / passed variants |
|---|---|---|---|---|---|---|
| 07b42fd | B | 1 | 1 | 1 | 0/3 | 0/3 |
| 0d8a4ee | A | 3 | 0 | 0 | 0/0 | 0/0 |
| 229360a | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 22cc237 | A | 3 | 3 | 2 | 4/8 | 0/4 |
| 29caf6f | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 37a8675 | A | 2 | 0 | 0 | 0/0 | 0/0 |
| 3c13f5a | B | 1 | 1 | 1 | 0/4 | 1/2 |
| 530b157 | B | 1 | 1 | 1 | 0/5 | 0/1 |
| 60d0b5b | B | 1 | 1 | 1 | 0/3 | 0/3 |
| 6c2c621 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 7d7fbf6 | B | 1 | 1 | 1 | 0/5 | 0/1 |
| aa8502b | B | 1 | 1 | 1 | 0/4 | 0/2 |
| afc0fce | B | 1 | 1 | 1 | 0/4 | 0/2 |
| b119b1f | B | 1 | 1 | 1 | 0/2 | 0/4 |
| c901732 | B | 1 | 1 | 1 | 0/3 | 0/3 |
| ccb4494 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| ce359b5 | A | 3 | 3 | 3 | 0/12 | 0/6 |
| cf6abd2 | B | 1 | 1 | 1 | 0/3 | 0/3 |
| d4e9306 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| e3d6c94 | B | 1 | 1 | 1 | 0/4 | 0/2 |

- scenarios: 20; with ≥1 evaluable task: 18; every evaluable task auto-accepted: 17/18; zero unsafe promotions: 17/18; mean per-scenario unsafe rate: 0.028
- DeltaSpec vs output_equal (agreement with official verdict): 121/126 = 0.96 [0.91, 0.98] vs 49/126 = 0.39 [0.31, 0.48]; McNemar b=73 c=1 p=7.94e-21; scenario-cluster bootstrap diff +0.571 [+0.485, +0.648]
- DeltaSpec vs hash_equal (agreement with official verdict): 121/126 = 0.96 [0.91, 0.98] vs 122/126 = 0.97 [0.92, 0.99]; McNemar b=3 c=4 p=1.00e+00; scenario-cluster bootstrap diff -0.008 [-0.043, +0.025]
- officially-failed variants (80): accepted by DeltaSpec 4, by output-equality 77, by state-hash 0
- officially-passed variants (46): blocked by DeltaSpec 1, by output-equality 0, by state-hash 4
- McNemar unsafe (DeltaSpec vs output-equality) b=0 c=73 p=2.12e-22; false blocks (DeltaSpec vs state-hash) b=0 c=3 p=2.50e-01
- vs v0.6.1 one-shot on 11 shared tasks / 66 variants: agreement 62/66 = 0.94 [0.85, 0.98] vs 48/66 = 0.73 [0.61, 0.82]; unsafe on failed variants 4 vs 18 of 44; McNemar p=1.22e-04; task-cluster bootstrap diff +0.212 [+0.121, +0.288]
- coverage on 22 evaluable tasks: DeltaSpec 21/22 = 0.95 [0.78, 0.99] vs v0.6.1 12/22 = 0.55 [0.35, 0.73]; McNemar b=10 c=1 p=1.17e-02

# Condition set `primary+retry` (deltaspec_fresh, deltaspec_fresh_retry_transport) — 27 rows, 132 paired variant cases

| scenario | tier | tasks | evaluable | auto-accept | unsafe / failed variants | false blocks / passed variants |
|---|---|---|---|---|---|---|
| 07b42fd | B | 1 | 1 | 1 | 0/3 | 0/3 |
| 0d8a4ee | A | 3 | 0 | 0 | 0/0 | 0/0 |
| 229360a | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 22cc237 | A | 3 | 3 | 3 | 6/12 | 0/6 |
| 29caf6f | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 37a8675 | A | 2 | 0 | 0 | 0/0 | 0/0 |
| 3c13f5a | B | 1 | 1 | 1 | 0/4 | 1/2 |
| 530b157 | B | 1 | 1 | 1 | 0/5 | 0/1 |
| 60d0b5b | B | 1 | 1 | 1 | 0/3 | 0/3 |
| 6c2c621 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 7d7fbf6 | B | 1 | 1 | 1 | 0/5 | 0/1 |
| aa8502b | B | 1 | 1 | 1 | 0/4 | 0/2 |
| afc0fce | B | 1 | 1 | 1 | 0/4 | 0/2 |
| b119b1f | B | 1 | 1 | 1 | 0/2 | 0/4 |
| c901732 | B | 1 | 1 | 1 | 0/3 | 0/3 |
| ccb4494 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| ce359b5 | A | 3 | 3 | 3 | 0/12 | 0/6 |
| cf6abd2 | B | 1 | 1 | 1 | 0/3 | 0/3 |
| d4e9306 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| e3d6c94 | B | 1 | 1 | 1 | 0/4 | 0/2 |

- scenarios: 20; with ≥1 evaluable task: 18; every evaluable task auto-accepted: 18/18; zero unsafe promotions: 17/18; mean per-scenario unsafe rate: 0.028
- DeltaSpec vs output_equal (agreement with official verdict): 125/132 = 0.95 [0.89, 0.97] vs 51/132 = 0.39 [0.31, 0.47]; McNemar b=75 c=1 p=2.04e-21; scenario-cluster bootstrap diff +0.561 [+0.472, +0.647]
- DeltaSpec vs hash_equal (agreement with official verdict): 125/132 = 0.95 [0.89, 0.97] vs 127/132 = 0.96 [0.91, 0.98]; McNemar b=4 c=6 p=7.54e-01; scenario-cluster bootstrap diff -0.015 [-0.058, +0.025]
- officially-failed variants (84): accepted by DeltaSpec 6, by output-equality 81, by state-hash 0
- officially-passed variants (48): blocked by DeltaSpec 1, by output-equality 0, by state-hash 5
- McNemar unsafe (DeltaSpec vs output-equality) b=0 c=75 p=5.29e-23; false blocks (DeltaSpec vs state-hash) b=0 c=4 p=1.25e-01
- vs v0.6.1 one-shot on 12 shared tasks / 72 variants: agreement 66/72 = 0.92 [0.83, 0.96] vs 52/72 = 0.72 [0.61, 0.81]; unsafe on failed variants 6 vs 20 of 48; McNemar p=1.22e-04; task-cluster bootstrap diff +0.194 [+0.111, +0.278]
- coverage on 22 evaluable tasks: DeltaSpec 22/22 = 1.00 [0.85, 1.00] vs v0.6.1 12/22 = 0.55 [0.35, 0.73]; McNemar b=10 c=0 p=1.95e-03

# Condition set `all` (deltaspec_fresh, deltaspec_fresh_retry_transport, deltaspec_fresh_rof) — 27 rows, 162 paired variant cases

| scenario | tier | tasks | evaluable | auto-accept | unsafe / failed variants | false blocks / passed variants |
|---|---|---|---|---|---|---|
| 07b42fd | B | 1 | 1 | 1 | 0/3 | 0/3 |
| 0d8a4ee | A | 3 | 3 | 3 | 0/15 | 0/3 |
| 229360a | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 22cc237 | A | 3 | 3 | 3 | 6/12 | 0/6 |
| 29caf6f | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 37a8675 | A | 2 | 2 | 2 | 0/6 | 0/6 |
| 3c13f5a | B | 1 | 1 | 1 | 0/4 | 1/2 |
| 530b157 | B | 1 | 1 | 1 | 0/5 | 0/1 |
| 60d0b5b | B | 1 | 1 | 1 | 0/3 | 0/3 |
| 6c2c621 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| 7d7fbf6 | B | 1 | 1 | 1 | 0/5 | 0/1 |
| aa8502b | B | 1 | 1 | 1 | 0/4 | 0/2 |
| afc0fce | B | 1 | 1 | 1 | 0/4 | 0/2 |
| b119b1f | B | 1 | 1 | 1 | 0/2 | 0/4 |
| c901732 | B | 1 | 1 | 1 | 0/3 | 0/3 |
| ccb4494 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| ce359b5 | A | 3 | 3 | 3 | 0/12 | 0/6 |
| cf6abd2 | B | 1 | 1 | 1 | 0/3 | 0/3 |
| d4e9306 | B | 1 | 1 | 1 | 0/4 | 0/2 |
| e3d6c94 | B | 1 | 1 | 1 | 0/4 | 0/2 |

- scenarios: 20; with ≥1 evaluable task: 20; every evaluable task auto-accepted: 20/20; zero unsafe promotions: 19/20; mean per-scenario unsafe rate: 0.025
- DeltaSpec vs output_equal (agreement with official verdict): 155/162 = 0.96 [0.91, 0.98] vs 60/162 = 0.37 [0.30, 0.45]; McNemar b=96 c=1 p=1.24e-27; scenario-cluster bootstrap diff +0.586 [+0.494, +0.673]
- DeltaSpec vs hash_equal (agreement with official verdict): 155/162 = 0.96 [0.91, 0.98] vs 157/162 = 0.97 [0.93, 0.99]; McNemar b=4 c=6 p=7.54e-01; scenario-cluster bootstrap diff -0.012 [-0.050, +0.018]
- officially-failed variants (105): accepted by DeltaSpec 6, by output-equality 102, by state-hash 0
- officially-passed variants (57): blocked by DeltaSpec 1, by output-equality 0, by state-hash 5
- McNemar unsafe (DeltaSpec vs output-equality) b=0 c=96 p=2.52e-29; false blocks (DeltaSpec vs state-hash) b=0 c=4 p=1.25e-01
- vs v0.6.1 one-shot on 15 shared tasks / 90 variants: agreement 84/90 = 0.93 [0.86, 0.97] vs 63/90 = 0.70 [0.60, 0.78]; unsafe on failed variants 6 vs 27 of 63; McNemar p=9.54e-07; task-cluster bootstrap diff +0.233 [+0.144, +0.311]
- coverage on 27 evaluable tasks: DeltaSpec 27/27 = 1.00 [0.88, 1.00] vs v0.6.1 15/27 = 0.56 [0.37, 0.72]; McNemar b=12 c=0 p=4.88e-04