
## Tier A (scenario-fresh): 11 rows, evaluable 6; classes {'invalid_reference(harness)': 5, 'auto_accept': 5, 'abstain(transport)': 1}
- auto-accept known-good: 5/6 = 0.83 [0.44,0.97] (of evaluable); 5/11 = 0.45 [0.21,0.72] of all rows
- incorrect blocks 0; abstain 1 (transport 1)
- unsafe promotions on officially-failed variants: 4/20  tasks ['22cc237_1', '22cc237_2']
- variant kill rate: 16/20 = 0.80 [0.58,0.92]
- false blocks on officially-passed variants: 0/10  tasks []
- synthetic mutant kill rate: 20/22 = 0.91 [0.72,0.97]
- state-hash baseline: unsafe 0/20, false blocks 2/10; output-text baseline: unsafe 20/20, false blocks 0/10
- v0.6.1 one-shot on same variants (5 tasks with a contract): accepts ref 5/5, unsafe 10/20, false blocks 0/10
  DeltaSpec on those same tasks: unsafe 4, false blocks 0
    0d8a4ee: 0d8a4ee_1 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_2 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_3 invalid_reference(harness) unsafe=0 fb=0
    22cc237: 22cc237_1 auto_accept unsafe=2 fb=0; 22cc237_2 auto_accept unsafe=2 fb=0; 22cc237_3 abstain(transport) unsafe=0 fb=0
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

## A ∪ B: 27 rows, evaluable 22; classes {'auto_accept': 21, 'invalid_reference(harness)': 5, 'abstain(transport)': 1}
- auto-accept known-good: 21/22 = 0.95 [0.78,0.99] (of evaluable); 21/27 = 0.78 [0.59,0.89] of all rows
- incorrect blocks 0; abstain 1 (transport 1)
- unsafe promotions on officially-failed variants: 4/80  tasks ['22cc237_1', '22cc237_2']
- variant kill rate: 76/80 = 0.95 [0.88,0.98]
- false blocks on officially-passed variants: 1/46  tasks ['3c13f5a_3']
- synthetic mutant kill rate: 85/100 = 0.85 [0.77,0.91]
- state-hash baseline: unsafe 0/80, false blocks 4/46; output-text baseline: unsafe 77/80, false blocks 0/46
- v0.6.1 one-shot on same variants (12 tasks with a contract): accepts ref 12/12, unsafe 20/48, false blocks 0/24
  DeltaSpec on those same tasks: unsafe 4, false blocks 0
    07b42fd: 07b42fd_3 auto_accept unsafe=0 fb=0
    0d8a4ee: 0d8a4ee_1 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_2 invalid_reference(harness) unsafe=0 fb=0; 0d8a4ee_3 invalid_reference(harness) unsafe=0 fb=0
    229360a: 229360a_2 auto_accept unsafe=0 fb=0
    22cc237: 22cc237_1 auto_accept unsafe=2 fb=0; 22cc237_2 auto_accept unsafe=2 fb=0; 22cc237_3 abstain(transport) unsafe=0 fb=0
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
