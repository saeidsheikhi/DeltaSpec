
## Tier A (scenario-fresh): 11 rows, evaluable 6; classes {'invalid_reference(harness)': 5, 'auto_accept': 6}
- auto-accept known-good: 6/6 = 1.00 [0.61,1.00] (of evaluable); 6/11 = 0.55 [0.28,0.79] of all rows
- incorrect blocks 0; abstain 0 (transport 0)
- unsafe promotions on officially-failed variants: 6/24  tasks ['22cc237_1', '22cc237_2', '22cc237_3']
- variant kill rate: 18/24 = 0.75 [0.55,0.88]
- false blocks on officially-passed variants: 0/12  tasks []
- synthetic mutant kill rate: 24/27 = 0.89 [0.72,0.96]
- state-hash baseline: unsafe 0/24, false blocks 3/12; output-text baseline: unsafe 24/24, false blocks 0/12
- v0.6.1 one-shot on same variants (5 tasks with a contract): accepts ref 5/5, unsafe 10/20, false blocks 0/10
  DeltaSpec on those same tasks: unsafe 6, false blocks 0
    0d8a4ee: 0d8a4ee_1 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_2 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_3 invalid_reference(harness) unsafe=0 fb=0
    22cc237: 22cc237_1 auto_accept unsafe=2 fb=0; 22cc237_2 auto_accept unsafe=2 fb=0; 22cc237_3 auto_accept unsafe=2 fb=0
    37a8675: 37a8675_2 invalid_reference(harness) unsafe=0 fb=0; 37a8675_3 invalid_reference(harness) unsafe=0 fb=0
    ce359b5: ce359b5_1 auto_accept unsafe=0 fb=0; ce359b5_2 auto_accept unsafe=0 fb=0; ce359b5_3 auto_accept unsafe=0 fb=0

## Tier B (task-fresh): 16 rows, evaluable 16; classes {'auto_accept': 16}
- auto-accept known-good: 16/16 = 1.00 [0.81,1.00] (of evaluable); 16/16 = 1.00 [0.81,1.00] of all rows
- incorrect blocks 0; abstain 0 (transport 0)
- unsafe promotions on officially-failed variants: 0/60  tasks []
- variant kill rate: 60/60 = 1.00 [0.94,1.00]
- false blocks on officially-passed variants: 1/36  tasks ['3c13f5a_3']
- synthetic mutant kill rate: 65/78 = 0.83 [0.74,0.90]
- state-hash baseline: unsafe 0/60, false blocks 2/36; output-text baseline: unsafe 57/60, false blocks 0/36
- v0.6.1 one-shot on same variants (7 tasks with a contract): accepts ref 7/7, unsafe 10/28, false blocks 0/14
  DeltaSpec on those same tasks: unsafe 0, false blocks 0
    07b42fd: 07b42fd_3 auto_accept unsafe=0 fb=0
    229360a: 229360a_2 auto_accept unsafe=0 fb=0
    29caf6f: 29caf6f_3 auto_accept unsafe=0 fb=0
    3c13f5a: 3c13f5a_3 auto_accept unsafe=0 fb=1
    530b157: 530b157_1 auto_accept unsafe=0 fb=0
    60d0b5b: 60d0b5b_3 auto_accept unsafe=0 fb=0
    6c2c621: 6c2c621_3 auto_accept unsafe=0 fb=0
    7d7fbf6: 7d7fbf6_3 auto_accept unsafe=0 fb=0
    aa8502b: aa8502b_1 auto_accept unsafe=0 fb=0
    afc0fce: afc0fce_3 auto_accept unsafe=0 fb=0
    b119b1f: b119b1f_3 auto_accept unsafe=0 fb=0
    c901732: c901732_1 auto_accept unsafe=0 fb=0
    ccb4494: ccb4494_3 auto_accept unsafe=0 fb=0
    cf6abd2: cf6abd2_3 auto_accept unsafe=0 fb=0
    d4e9306: d4e9306_1 auto_accept unsafe=0 fb=0
    e3d6c94: e3d6c94_2 auto_accept unsafe=0 fb=0

## A ∪ B: 27 rows, evaluable 22; classes {'auto_accept': 22, 'invalid_reference(harness)': 5}
- auto-accept known-good: 22/22 = 1.00 [0.85,1.00] (of evaluable); 22/27 = 0.81 [0.63,0.92] of all rows
- incorrect blocks 0; abstain 0 (transport 0)
- unsafe promotions on officially-failed variants: 6/84  tasks ['22cc237_1', '22cc237_2', '22cc237_3']
- variant kill rate: 78/84 = 0.93 [0.85,0.97]
- false blocks on officially-passed variants: 1/48  tasks ['3c13f5a_3']
- synthetic mutant kill rate: 89/105 = 0.85 [0.77,0.90]
- state-hash baseline: unsafe 0/84, false blocks 5/48; output-text baseline: unsafe 81/84, false blocks 0/48
- v0.6.1 one-shot on same variants (12 tasks with a contract): accepts ref 12/12, unsafe 20/48, false blocks 0/24
  DeltaSpec on those same tasks: unsafe 6, false blocks 0
    07b42fd: 07b42fd_3 auto_accept unsafe=0 fb=0
    0d8a4ee: 0d8a4ee_1 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_2 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_3 invalid_reference(harness) unsafe=0 fb=0
    229360a: 229360a_2 auto_accept unsafe=0 fb=0
    22cc237: 22cc237_1 auto_accept unsafe=2 fb=0; 22cc237_2 auto_accept unsafe=2 fb=0; 22cc237_3 auto_accept unsafe=2 fb=0
    29caf6f: 29caf6f_3 auto_accept unsafe=0 fb=0
    37a8675: 37a8675_2 invalid_reference(harness) unsafe=0 fb=0; 37a8675_3 invalid_reference(harness) unsafe=0 fb=0
    3c13f5a: 3c13f5a_3 auto_accept unsafe=0 fb=1
    530b157: 530b157_1 auto_accept unsafe=0 fb=0
    60d0b5b: 60d0b5b_3 auto_accept unsafe=0 fb=0
    6c2c621: 6c2c621_3 auto_accept unsafe=0 fb=0
    7d7fbf6: 7d7fbf6_3 auto_accept unsafe=0 fb=0
    aa8502b: aa8502b_1 auto_accept unsafe=0 fb=0
    afc0fce: afc0fce_3 auto_accept unsafe=0 fb=0
    b119b1f: b119b1f_3 auto_accept unsafe=0 fb=0
    c901732: c901732_1 auto_accept unsafe=0 fb=0
    ccb4494: ccb4494_3 auto_accept unsafe=0 fb=0
    ce359b5: ce359b5_1 auto_accept unsafe=0 fb=0; ce359b5_2 auto_accept unsafe=0 fb=0; ce359b5_3 auto_accept unsafe=0 fb=0
    cf6abd2: cf6abd2_3 auto_accept unsafe=0 fb=0
    d4e9306: d4e9306_1 auto_accept unsafe=0 fb=0
    e3d6c94: e3d6c94_2 auto_accept unsafe=0 fb=0
