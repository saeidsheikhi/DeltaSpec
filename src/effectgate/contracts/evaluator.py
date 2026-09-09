"""Deterministic interpreter for the restricted Effect Contract DSL.

Design rules (see ARCHITECTURE.md / EFFECT_CONTRACT_SPEC.md):

* No arbitrary code execution. Only the operators in ``ALLOWED_OPS`` (28 as of DSL
  v0.4; each version adds operators and keeps older contracts valid). v0.4 adds
  ``record``/``scope`` kinds over a typed field projection (see fieldview.py).
* Total and deterministic: same (contract, S0, S1) always gives the same verdict.
* Fail closed. Anything the interpreter cannot evaluate raises ``ContractEvalError``
  and is surfaced as an ``error`` status, never as PASS.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from effectgate.models import (
    ALLOWED_KINDS,
    ALLOWED_OPS,
    DELTA_ONLY_OPS,
    RECORD_DELTA_OPS,
    ContractEvalError,
    EffectContract,
    Predicate,
)
from effectgate.contracts.fieldview import eval_v04_predicate


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<MISSING>"


MISSING = _Missing()


def get_path(obj: Any, path: str) -> Any:
    """Resolve a JSON-Pointer-like path (``/a/b``) or a simple dotted path (``a.b``).

    JSON Pointer form is preferred because object keys may themselves contain dots.
    Returns :data:`MISSING` when the path does not resolve.
    """
    cur = obj
    if path in ("", "$", "/"):
        return cur
    if path.startswith("/"):
        parts = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]
    else:
        parts = [p for p in path.split(".") if p not in ("", "$")]
    for part in parts:
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            return MISSING
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return MISSING
        return MISSING
    return cur


def _as_set(x: Any) -> set:
    """Best-effort hashable set view used by ``subset`` / ``set_eq``."""
    if isinstance(x, dict):
        return set(x.keys())
    if isinstance(x, (list, tuple, set)):
        out = set()
        for el in x:
            out.add(el if isinstance(el, (str, int, float, bool, type(None))) else repr(el))
        return out
    if isinstance(x, str):
        return {x}
    raise ContractEvalError(f"value of type {type(x).__name__} cannot be treated as a set")


def _contains(got: Any, exp: Any) -> bool:
    """Documented ``contains`` semantics.

    * ``str`` haystack: substring test.
    * ``list``/``tuple`` haystack: exact element match, or substring match against
      any string element (so ``contains "invoice"`` matches ``"invoice sent"``).
    * ``dict`` haystack: key membership.
    """
    if isinstance(got, str):
        return isinstance(exp, str) and exp in got
    if isinstance(got, dict):
        return exp in got
    if isinstance(got, (list, tuple, set)):
        for el in got:
            if el == exp:
                return True
            if isinstance(el, str) and isinstance(exp, str) and exp in el:
                return True
        return False
    return False


def _length(got: Any) -> int:
    try:
        return len(got)
    except TypeError as exc:
        raise ContractEvalError(f"count operator applied to non-sized value {type(got).__name__}") from exc


def _eval_state_pred(pred: Predicate, state: dict) -> bool:
    got = get_path(state, pred.path)
    op, exp = pred.op, pred.value

    if op == "exists":
        return got is not MISSING
    if op == "absent":
        return got is MISSING
    if got is MISSING:
        # Every remaining operator is a property of an existing value.
        return False
    if op == "eq":
        return got == exp
    if op == "ne":
        return got != exp
    if op == "contains":
        return _contains(got, exp)
    if op == "not_contains":
        return not _contains(got, exp)
    if op == "count_eq":
        return _length(got) == int(exp)
    if op == "count_ge":
        return _length(got) >= int(exp)
    if op == "subset":
        return _as_set(got).issubset(_as_set(exp))
    if op == "set_eq":
        return _as_set(got) == _as_set(exp)
    raise ContractEvalError(f"operator '{op}' is not valid for kind='state'")


def eval_predicate(pred: Predicate, initial_state: dict, final_state: dict) -> bool:
    """Evaluate one predicate over the transition S0 -> S1. Never returns None."""
    if pred.kind not in ALLOWED_KINDS:
        raise ContractEvalError(f"unknown predicate kind '{pred.kind}'")
    if pred.op not in ALLOWED_OPS:
        raise ContractEvalError(f"unknown operator '{pred.op}'")

    # Semantics are determined by the *operator*, not by ``kind``. ``changed`` and
    # ``unchanged`` compare S0 with S1; every other operator is a property of S1.
    # ``kind`` is advisory: a kind/operator mismatch is a lint warning (and is counted
    # as a compiler-quality metric), not an evaluation error, because the intended
    # meaning is unambiguous either way. See notes/DECISIONS.md (2026-08-11).
    # v0.4: typed record selectors and change-scope predicates live in fieldview.py.
    # Dispatch on *kind* here because `exists`/`count_ge` are shared operator names
    # whose meaning differs by kind: a path presence test vs. a typed selector match.
    if pred.kind in ("record", "scope"):
        return eval_v04_predicate(pred, initial_state, final_state)

    if pred.op in RECORD_DELTA_OPS:
        return _eval_record_delta(pred, initial_state, final_state)

    if pred.op in DELTA_ONLY_OPS:
        before = get_path(initial_state, pred.path)
        after = get_path(final_state, pred.path)
        if pred.op == "unchanged":
            return before == after
        return before != after
    return _eval_state_pred(pred, final_state)


def _eval_record_delta(pred: Predicate, initial_state: dict, final_state: dict) -> bool:
    """How many records a table gained, lost, or had edited in place.

    The path must resolve to a ``{record_id: record_hash}`` mapping in both states.
    An absent table is treated as empty on that side, so table creation and deletion
    are expressible; a non-mapping value is an evaluation error, not a silent False.
    """
    before = get_path(initial_state, pred.path)
    after = get_path(final_state, pred.path)
    if before is MISSING:
        before = {}
    if after is MISSING:
        after = {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ContractEvalError(
            f"operator '{pred.op}' needs a {{record_id: record_hash}} mapping at "
            f"'{pred.path}', got {type(after).__name__}")
    try:
        expected = int(pred.value)
    except (TypeError, ValueError) as exc:
        raise ContractEvalError(
            f"operator '{pred.op}' requires an integer value, got {pred.value!r}") from exc

    b, a = set(before), set(after)
    n = {
        "added": len(a - b),
        "removed": len(b - a),
        "updated": len([k for k in a & b if before[k] != after[k]]),
    }[pred.op.split("_", 1)[0]]
    return n >= expected if pred.op.endswith("_ge") else n == expected


@dataclass
class PredicateOutcome:
    predicate: Predicate
    group: str          # required | forbidden | invariants | alternatives[i]
    raw: bool | None    # truth value of the predicate itself (None when it errored)
    satisfied: bool     # whether the contract clause is satisfied
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "predicate": self.predicate.to_dict(),
            "raw": self.raw,
            "satisfied": self.satisfied,
            "error": self.error,
        }


@dataclass
class ContractResult:
    """Outcome of evaluating a whole contract.

    ``status`` is one of ``pass`` / ``fail`` / ``error``. ``error`` means the
    contract could not be deterministically interpreted; under the release policy
    it is never treated as a pass.
    """
    passed: bool
    status: str
    outcomes: list[PredicateOutcome]
    alternatives_passed: bool | None
    errors: list[str]

    # Backwards-compatible views used by earlier scripts/tests.
    @property
    def required(self) -> list[tuple[Predicate, bool]]:
        return [(o.predicate, o.satisfied) for o in self.outcomes if o.group == "required"]

    @property
    def forbidden(self) -> list[tuple[Predicate, bool]]:
        return [(o.predicate, o.satisfied) for o in self.outcomes if o.group == "forbidden"]

    @property
    def invariants(self) -> list[tuple[Predicate, bool]]:
        return [(o.predicate, o.satisfied) for o in self.outcomes if o.group == "invariants"]

    @property
    def critical_passed(self) -> bool:
        conj = [
            o for o in self.outcomes
            if o.predicate.critical and not o.group.startswith("alternatives[")
        ]
        ok = all(o.satisfied for o in conj)
        if self.alternatives_passed is False:
            ok = False
        return ok and not self.errors

    def violations(self) -> list[PredicateOutcome]:
        return [
            o for o in self.outcomes
            if not o.satisfied and not o.group.startswith("alternatives[")
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status,
            "alternatives_passed": self.alternatives_passed,
            "errors": list(self.errors),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def evaluate_contract(
    contract: EffectContract,
    initial_state: dict,
    final_state: dict,
    fail_closed: bool = True,
) -> ContractResult:
    """Evaluate ``contract`` over the transition ``initial_state -> final_state``.

    ``required`` and ``invariants`` predicates must hold. ``forbidden`` predicates
    describe prohibited conditions, so the clause is satisfied when the predicate is
    false. ``alternatives`` is a disjunction of conjunctive groups; when present at
    least one group must hold.
    """
    outcomes: list[PredicateOutcome] = []
    errors: list[str] = []

    def run(pred: Predicate, group: str, negate: bool) -> bool | None:
        try:
            raw = eval_predicate(pred, initial_state, final_state)
        except Exception as exc:  # deterministic containment of interpreter failures
            msg = f"{group}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            outcomes.append(PredicateOutcome(pred, group, None, not fail_closed, msg))
            return None
        satisfied = (not raw) if negate else raw
        outcomes.append(PredicateOutcome(pred, group, raw, satisfied))
        return raw

    for p in contract.required:
        run(p, "required", negate=False)
    for p in contract.forbidden:
        run(p, "forbidden", negate=True)
    for p in contract.invariants:
        run(p, "invariants", negate=False)

    alt_pass: bool | None = None
    if contract.alternatives:
        group_results: list[bool] = []
        for i, grp in enumerate(contract.alternatives):
            vals: list[bool | None] = [run(p, f"alternatives[{i}]", negate=False) for p in grp]
            group_results.append(bool(vals) and all(v is True for v in vals))
        alt_pass = any(group_results)

    # Alternatives are a disjunction: an individually failing group must not sink
    # the whole contract, so they are excluded from the conjunctive tally.
    passed = all(o.satisfied for o in outcomes if not o.group.startswith("alternatives["))
    if alt_pass is not None:
        passed = passed and alt_pass

    if errors:
        status = "error"
        if fail_closed:
            passed = False
    else:
        status = "pass" if passed else "fail"
    return ContractResult(passed, status, outcomes, alt_pass, errors)
