# Frozen variants of `scripts/appworld_worker.py`

The two frozen systems reported in the paper differ in exactly one line of this file:

| frozen record | `AppWorld(...)` constructor | used for |
|---|---|---|
| `results/frozen_systems/deltaspec_v1.json` (**primary**) | library default, `raise_on_failure=True` | the primary validation table (`deltaspec_fresh`, transport retry, direct-authoring baseline `linked_v061_freshB`) |
| `results/frozen_systems/deltaspec_v1_rof.json` (**post-hoc**) | `raise_on_failure=False` (as in AppWorld's own verifier) | the post-hoc harness condition (`deltaspec_fresh_rof`, `linked_v061_freshB_rof`) and the supplementary robustness/ablation run |

`appworld_worker.deltaspec_v1.py` and `appworld_worker.deltaspec_v1_rof.py` are byte-exact copies
(their SHA-256 values are the ones recorded in the two freeze records);
`appworld_worker.v1_to_rof.patch` is the unified diff between them. The shipped
`scripts/appworld_worker.py` is the **post-hoc (`_rof`) version**. See the top-level README for
the switch/verify commands.
