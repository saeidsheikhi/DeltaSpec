"""DeltaSpec — intent-labelled effect specifications mined from reference executions.

Replaces EffectGate's LLM contract *compiler* with:

    mine      deterministic, schema-typed candidate facts from (pre-state, known-good
              post-state, delta, schema)                      -> src/deltaspec/mine.py
    label     one short LLM call classifying each fact as required / side_effect /
              incidental                                       -> src/deltaspec/label.py
    assemble  labels -> a declarative DSL v0.5 contract (required, forbidden, scope)
              with abstention rules                            -> src/deltaspec/assemble.py
    validate  synthetic state mutants (missing / extra / wrong value / collateral) that
              the assembled contract must reject; kill rate     -> src/deltaspec/mutants.py

The gate (restorable replay, deterministic evaluator, fail-closed policy) is EffectGate's,
unchanged. Rationale: notes/ARCHITECTURE_REVIEW.md.
"""
