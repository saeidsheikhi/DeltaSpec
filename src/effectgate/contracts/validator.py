"""Counterexample-guided self-validation and repair of generated effect contracts.

The loop is deliberately **gold-free**: it may look only at

* the known-good final state produced by the currently-promoted (reference) agent
  version, and
* synthetic counterfactual end states with labels declared by the mutation engine.

It never consults the gold oracle, so any improvement it produces is an improvement
the system could have made in deployment. Scoring against gold happens afterwards,
on a *held-out* mutant split, so that repair cannot overfit the measurement set.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from effectgate.contracts.compiler import REPAIR_PROMPT, finalize
from effectgate.contracts.evaluator import MISSING, evaluate_contract, get_path
from effectgate.contracts.io import contract_hash
from effectgate.contracts.schema import ollama_format_schema
from effectgate.models import EffectContract
from effectgate.mutations.engine import StateMutant
from effectgate.world.engine import state_diff

MAX_COUNTEREXAMPLES_IN_PROMPT = 6
MAX_DIFF_ENTRIES = 8


def _describe(value: Any) -> Any:
    """Render a resolved path value for the repair prompt.

    ``MISSING`` must be reported explicitly: "this path does not exist" is the single
    most useful fact when a contract asserts on a path shape the state does not have.
    """
    if value is MISSING:
        return "<<THIS PATH DOES NOT EXIST IN THE STATE>>"
    if isinstance(value, (dict, list)) and len(repr(value)) > 300:
        if isinstance(value, dict):
            return {"<<truncated object, keys>>": sorted(value)[:20]}
        return {"<<truncated list, length>>": len(value)}
    return value


@dataclass
class Counterexample:
    kind: str                    # false_accept | false_reject | rejects_known_good
    mutation_id: str
    family: str
    diff_from_known_good: list[dict[str, Any]]
    #: For ``rejects_known_good``: the offending predicates together with the value
    #: actually present at each path. Without this the model is told only that it
    #: rejected a correct outcome, which is not enough to locate the mistake.
    failing_predicates: list[dict[str, Any]] = field(default_factory=list)

    def to_prompt_obj(self) -> dict[str, Any]:
        if self.kind == "rejects_known_good":
            return {
                "problem": (
                    "YOUR CONTRACT REJECTED THE KNOWN-GOOD END STATE. This is the "
                    "state a correct execution of the task actually produced, so "
                    "every predicate listed below is wrong and must be corrected or "
                    "removed. Fix these first: a contract that rejects correct "
                    "behaviour blocks every release."
                ),
                "predicates_of_yours_that_are_false_in_the_correct_end_state":
                    self.failing_predicates[:MAX_DIFF_ENTRIES],
            }
        if self.kind == "false_accept":
            return {
                "problem": "your contract ACCEPTED this end state, but it is invalid",
                "defect_family": self.family,
                "how_this_state_differs_from_the_known_good_end_state":
                    self.diff_from_known_good[:MAX_DIFF_ENTRIES],
            }
        return {
            "problem": "your contract REJECTED this end state, but it is also correct",
            "defect_family": self.family,
            "how_this_state_differs_from_the_known_good_end_state":
                self.diff_from_known_good[:MAX_DIFF_ENTRIES],
        }

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "mutation_id": self.mutation_id, "family": self.family,
                "diff": self.diff_from_known_good[:MAX_DIFF_ENTRIES],
                "failing_predicates": self.failing_predicates[:MAX_DIFF_ENTRIES]}


@dataclass
class ValidationRound:
    round: int
    contract_hash: str
    accepts_known_good: bool
    n_invalid: int
    n_valid: int
    false_accepts: int
    false_rejects: int
    eval_errors: int
    counterexamples: list[Counterexample] = field(default_factory=list)
    repair_status: str | None = None
    repair_error: str | None = None

    @property
    def far(self) -> float | None:
        return self.false_accepts / self.n_invalid if self.n_invalid else None

    @property
    def frr(self) -> float | None:
        return self.false_rejects / self.n_valid if self.n_valid else None

    @property
    def clean(self) -> bool:
        return (self.accepts_known_good and self.false_accepts == 0
                and self.false_rejects == 0 and self.eval_errors == 0)

    def cost(self) -> tuple[int, int, int, int]:
        """Lexicographic badness, lower is better."""
        return (0 if self.accepts_known_good else 1, self.eval_errors,
                self.false_accepts, self.false_rejects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round, "contract_hash": self.contract_hash,
            "accepts_known_good": self.accepts_known_good,
            "n_invalid": self.n_invalid, "n_valid": self.n_valid,
            "false_accepts": self.false_accepts, "false_rejects": self.false_rejects,
            "eval_errors": self.eval_errors, "far": self.far, "frr": self.frr,
            "clean": self.clean, "repair_status": self.repair_status,
            "repair_error": self.repair_error,
            "counterexamples": [c.to_dict() for c in self.counterexamples],
        }


@dataclass
class ValidationOutcome:
    task_id: str
    frozen_contract: EffectContract | None
    frozen: bool
    rounds: list[ValidationRound]
    repair_rounds_used: int
    provider_errors: int = 0

    @property
    def initial_round(self) -> ValidationRound | None:
        return self.rounds[0] if self.rounds else None

    @property
    def final_round(self) -> ValidationRound | None:
        return self.rounds[-1] if self.rounds else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "frozen": self.frozen,
            "frozen_contract_hash": contract_hash(self.frozen_contract) if self.frozen_contract else None,
            "repair_rounds_used": self.repair_rounds_used,
            "provider_errors": self.provider_errors,
            "rounds": [r.to_dict() for r in self.rounds],
        }


def score_contract(
    contract: EffectContract,
    initial_state: dict[str, Any],
    known_good: dict[str, Any],
    invalid: list[StateMutant],
    valid: list[StateMutant],
    round_index: int = 0,
    collect_counterexamples: bool = True,
) -> ValidationRound:
    """Evaluate one contract against the declared-label mutant suite."""
    r_good = evaluate_contract(contract, initial_state, known_good)
    errors = 1 if r_good.status == "error" else 0
    ces: list[Counterexample] = []
    if not r_good.passed and collect_counterexamples:
        ces.append(Counterexample(
            "rejects_known_good", "known_good", "reference_behaviour",
            diff_from_known_good=[],
            failing_predicates=[
                {
                    "clause": o.group,
                    "your_predicate": o.predicate.to_dict(),
                    "value_actually_at_this_path_in_the_correct_end_state":
                        _describe(get_path(known_good, o.predicate.path)),
                    "value_at_this_path_before_the_task_ran":
                        _describe(get_path(initial_state, o.predicate.path)),
                    "error": o.error,
                }
                for o in r_good.violations()
            ],
        ))

    fa = fr = 0
    for m in invalid:
        res = evaluate_contract(contract, initial_state, m.state)
        errors += int(res.status == "error")
        if res.passed:
            fa += 1
            if collect_counterexamples:
                ces.append(Counterexample("false_accept", m.mutation_id, m.family,
                                          state_diff(known_good, m.state)))
    for m in valid:
        res = evaluate_contract(contract, initial_state, m.state)
        errors += int(res.status == "error")
        if not res.passed:
            fr += 1
            if collect_counterexamples:
                ces.append(Counterexample("false_reject", m.mutation_id, m.family,
                                          state_diff(known_good, m.state)))

    return ValidationRound(
        round=round_index, contract_hash=contract_hash(contract),
        accepts_known_good=r_good.passed, n_invalid=len(invalid), n_valid=len(valid),
        false_accepts=fa, false_rejects=fr, eval_errors=errors, counterexamples=ces,
    )


def _select_counterexamples(ces: list[Counterexample]) -> list[Counterexample]:
    """Prefer one counterexample per defect family so repair does not chase duplicates."""
    picked: list[Counterexample] = []
    seen: set[tuple[str, str]] = set()
    for c in ces:
        key = (c.kind, c.family)
        if key in seen:
            continue
        seen.add(key)
        picked.append(c)
        if len(picked) >= MAX_COUNTEREXAMPLES_IN_PROMPT:
            break
    for c in ces:
        if len(picked) >= MAX_COUNTEREXAMPLES_IN_PROMPT:
            break
        if c not in picked:
            picked.append(c)
    return picked


def repair_contract(
    provider: Any,
    model: str,
    task_id: str,
    instruction: str,
    initial_state: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    contract: EffectContract,
    counterexamples: list[Counterexample],
    known_good_diff: list[dict[str, Any]],
    known_good: dict[str, Any] | None = None,
    lint_state: dict[str, Any] | None = None,
    prompt_path: str | Path = REPAIR_PROMPT,
    seed: int | None = 42,
    temperature: float = 0.0,
    num_predict: int = 1536,
    num_ctx: int | None = 8192,
):
    """Ask the compiler model to revise a contract given deterministic counterexamples."""
    system = Path(prompt_path).read_text(encoding="utf-8")
    payload = {
        "task_id": task_id,
        "instruction": instruction,
        "initial_state": initial_state,
        "tool_schemas": tool_schemas,
        "effects_of_the_known_good_execution": known_good_diff[:20],
        "current_contract": contract.to_dict(),
        "counterexamples": [c.to_prompt_obj() for c in _select_counterexamples(counterexamples)],
    }
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, sort_keys=True, indent=1)},
    ]
    result = provider.chat(
        model, messages, temperature=temperature, num_predict=num_predict,
        seed=seed, num_ctx=num_ctx, format=ollama_format_schema(),
    )
    # Pre-flight the repaired contract against the known-good state, and lint it
    # against the state it will be *evaluated* on. `lint_state` matters wherever the
    # compiler's view differs from the evaluation view (AppWorld shows a summarised
    # view): linting a repair against the prompt view rejects every correct path it
    # writes, which silently broke the first AppWorld pilot.
    return finalize(task_id, model, result, initial_state,
                    reference_final_state=known_good, lint_state=lint_state)


SYNTAX_REPAIR_PROMPT = REPAIR_PROMPT.parent / "contract_repair_syntax.md"


def repair_syntax(
    provider: Any,
    model: str,
    task_id: str,
    instruction: str,
    initial_state: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    failed: Any,
    prompt_path: str | Path = SYNTAX_REPAIR_PROMPT,
    seed: int | None = 42,
    num_predict: int = 1536,
    num_ctx: int | None = 8192,
    lint_state: dict[str, Any] | None = None,
    reference_final_state: dict[str, Any] | None = None,
):
    """One mechanical-repair round for a contract that failed schema or static lint.

    This is the "one-shot + syntax/schema validation" condition of RQ2. It is not
    counterexample-guided: the model sees only deterministic validator errors, never
    a mutant and never the gold oracle.

    `lint_state` separates what the model is *shown* from what the contract is
    *checked against*, as `compile_contract` does. AppWorld needs both: the prompt view
    is 9.7 KB while the evaluation view is 8.8 MB, and linting against the prompt view
    rejects every correct `/records/...` path as an unknown root.
    """
    system = Path(prompt_path).read_text(encoding="utf-8")
    errors: list[str] = list(getattr(failed, "schema_errors", []) or [])
    if getattr(failed, "lint", None) is not None:
        errors += list(failed.lint.errors)
    if not errors and failed.error:
        errors = [str(failed.error)]

    payload = {
        "task_id": task_id,
        "instruction": instruction,
        "state_top_level_keys": sorted(initial_state.keys()),
        "initial_state": initial_state,
        "tool_schemas": tool_schemas,
        "rejected_contract": failed.contract_dict if failed.contract_dict is not None
        else failed.raw_text[:4000],
        "validator_errors": errors[:20],
    }
    result = provider.chat(
        model,
        [{"role": "system", "content": system},
         {"role": "user", "content": json.dumps(payload, sort_keys=True, indent=1)}],
        temperature=0.0, num_predict=num_predict, seed=seed, num_ctx=num_ctx,
        format=ollama_format_schema(),
    )
    return finalize(task_id, model, result, initial_state,
                    lint_state=lint_state, reference_final_state=reference_final_state)


def self_validate(
    provider: Any,
    model: str,
    task_id: str,
    instruction: str,
    initial_state: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    contract: EffectContract,
    known_good: dict[str, Any],
    invalid: list[StateMutant],
    valid: list[StateMutant],
    max_repair_rounds: int = 2,
    on_call: Any = None,
) -> ValidationOutcome:
    """Run the counterexample-guided repair loop and freeze the best contract found."""
    known_good_diff = state_diff(initial_state, known_good)
    rounds: list[ValidationRound] = []
    provider_errors = 0

    current = contract
    best = contract
    r = score_contract(current, initial_state, known_good, invalid, valid, 0)
    rounds.append(r)
    best_cost = r.cost()
    used = 0

    while used < max_repair_rounds and not rounds[-1].clean:
        used += 1
        out = repair_contract(
            provider, model, task_id, instruction, initial_state, tool_schemas,
            current, rounds[-1].counterexamples, known_good_diff, known_good,
        )
        if on_call is not None and out.llm is not None:
            on_call(out, used)
        if out.status == "provider_error":
            provider_errors += 1
        if not out.ok or out.contract is None:
            rounds[-1].repair_status = out.status
            rounds[-1].repair_error = out.error
            break
        rounds[-1].repair_status = "ok"
        current = out.contract
        r = score_contract(current, initial_state, known_good, invalid, valid, used)
        rounds.append(r)
        if r.cost() < best_cost:
            best, best_cost = current, r.cost()

    return ValidationOutcome(
        task_id=task_id,
        frozen_contract=best,
        frozen=best is not None,
        rounds=rounds,
        repair_rounds_used=used,
        provider_errors=provider_errors,
    )
