"""Counterfactual end-state mutation engine.

Mutation testing here targets the **oracle**, not the agent: given a known-good
final state, we synthesise nearby states that a correct effect contract must
reject (and valid variations it must accept), then measure how many of them the
contract actually kills.

Two generators are compared throughout the experiments (MUTATION_TAXONOMY.md):

``targeted``  task-aware: derived from the task's effect footprint (which leaves a
              correct execution changed) and from the surrounding in-scope state;
``random``    task-unaware: uniform perturbations of arbitrary leaves.

Every mutant carries a *declared* label derived from the transform's semantics.
The declared label is what self-validation is allowed to see. Experiment scoring
additionally computes the gold-oracle label and reports label agreement, so the
noisiness of each generator is measured rather than assumed.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any

from effectgate.contracts.io import state_hash

INVALID_FAMILIES = (
    "omission", "wrong_entity", "wrong_value", "near_miss", "duplicate_effect",
    "collateral_deletion", "collateral_modification", "extra_creation",
    "partial_completion", "state_staleness", "scope_escape", "forbidden_transition",
)
VALID_FAMILIES = ("reordering", "equivalent_phrasing", "alternate_valid_trajectory")


# ------------------------------------------------------------------ path utils
def _split(path: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]


def _esc(k: str) -> str:
    return str(k).replace("~", "~0").replace("/", "~1")


_SENTINEL = object()


def pget(obj: Any, path: str, default: Any = _SENTINEL) -> Any:
    cur = obj
    for part in _split(path):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def pexists(obj: Any, path: str) -> bool:
    return pget(obj, path, _SENTINEL) is not _SENTINEL


def pset(obj: Any, path: str, value: Any) -> bool:
    parts = _split(path)
    cur = obj
    for part in parts[:-1]:
        if isinstance(cur, dict):
            cur = cur.setdefault(part, {})
        else:
            return False
    if isinstance(cur, dict):
        cur[parts[-1]] = value
        return True
    return False


def pdel(obj: Any, path: str) -> bool:
    parts = _split(path)
    cur = obj
    for part in parts[:-1]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    if isinstance(cur, dict) and parts[-1] in cur:
        cur.pop(parts[-1])
        return True
    return False


def parent_of(path: str) -> str:
    return "/".join(path.split("/")[:-1])


def leaf_paths(obj: Any, prefix: str = "") -> list[str]:
    """All non-dict leaves (lists count as leaves; message lists are atomic values)."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(leaf_paths(v, f"{prefix}/{_esc(k)}"))
    elif prefix:
        out.append(prefix)
    return out


def object_paths(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 3) -> list[str]:
    """Paths of dict nodes below the root (candidate 'objects' to delete/create in)."""
    out: list[str] = []
    if isinstance(obj, dict) and depth <= max_depth:
        if prefix:
            out.append(prefix)
        for k, v in obj.items():
            out.extend(object_paths(v, f"{prefix}/{_esc(k)}", depth + 1, max_depth))
    return out


# ---------------------------------------------------------------------- mutants
@dataclass
class StateMutant:
    mutation_id: str
    family: str
    expected_valid: bool
    state: dict[str, Any]
    note: str = ""
    generator: str = "targeted"
    target_path: str | None = None
    parent_hash: str = ""
    gold_valid: bool | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "family": self.family,
            "expected_valid": self.expected_valid,
            "gold_valid": self.gold_valid,
            "note": self.note,
            "generator": self.generator,
            "target_path": self.target_path,
            "parent_hash": self.parent_hash,
            "state_hash": state_hash(self.state),
        }


def _perturb_value(v: Any, rng: random.Random, near: bool) -> Any:
    """A different value of the same type. ``near`` produces a plausible near miss."""
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 1 if near else v * 2 + 7
    if isinstance(v, float):
        return v + 0.01 if near else v * 2 + 7.0
    if isinstance(v, str):
        if near:
            digits = [i for i, c in enumerate(v) if c.isdigit()]
            if digits:
                i = digits[-1]
                return v[:i] + str((int(v[i]) + 1) % 10) + v[i + 1:]
            if len(v) > 3:
                i = len(v) // 2
                return v[:i] + v[i + 1:]          # dropped character
            return v + "_"
        return f"CHANGED_{rng.randrange(1000)}"
    if isinstance(v, list):
        if not v:
            return ["unexpected entry"]
        out = list(v)
        out[-1] = _perturb_value(out[-1], rng, near) if isinstance(out[-1], (str, int, float, bool)) \
            else "unexpected entry"
        return out
    if isinstance(v, dict):
        out = dict(v)
        out["_unexpected"] = True
        return out
    return "CHANGED"


#: Only *explicitly quoted* literals are treated as mandatory message content.
_QUOTED = [r"'([^']{1,60})'", r'"([^"]{1,60})"']


def salient_tokens(instruction: str) -> list[str]:
    """Literals the task instruction quotes verbatim.

    Free-text effects (a notification body) may legitimately be worded any way the
    agent likes, so perturbing them at random produces mislabelled mutants. The one
    thing an instruction can unambiguously demand of a message body is a literal it
    puts in quotes, so that is the only content the mutation engine perturbs. This
    uses the instruction only -- the same input the compiler gets -- so it stays
    gold-free. See notes/DECISIONS.md (2026-08-11, free-text mutation scope).
    """
    import re
    toks: list[str] = []
    for pat in _QUOTED:
        for m in re.finditer(pat, instruction):
            tok = m.group(1).strip()
            if tok and tok not in toks and len(tok) >= 2:
                toks.append(tok)
    return toks


def _is_free_text(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) for x in v)


def _perturb_free_text(v: list[str], tokens: list[str], rng: random.Random, near: bool) -> Any | None:
    """Perturb a free-text stream only where it carries a task-named literal."""
    for i, msg in enumerate(v):
        for tok in tokens:
            if tok in msg:
                new_tok = _perturb_value(tok, rng, near)
                out = list(v)
                out[i] = msg.replace(tok, str(new_tok), 1)
                return out
    return None


def _mk(state: dict, family: str, valid: bool, note: str, idx: int,
        generator: str, target: str | None, parent_hash: str) -> StateMutant:
    return StateMutant(
        mutation_id=f"{generator[:3]}-{family}-{idx}",
        family=family, expected_valid=valid, state=state, note=note,
        generator=generator, target_path=target, parent_hash=parent_hash,
    )


def generate_targeted_mutants(
    initial_state: dict[str, Any],
    good_state: dict[str, Any],
    footprint: set[str],
    rng: random.Random,
    budget: int = 16,
    instruction: str = "",
) -> list[StateMutant]:
    """Task-aware counterfactual end states derived from the effect footprint."""
    ph = state_hash(good_state)
    tokens = salient_tokens(instruction)
    out: list[StateMutant] = []
    changed = sorted(p for p in footprint if pexists(good_state, p) or pexists(initial_state, p))
    all_leaves = leaf_paths(good_state)
    outside = [p for p in all_leaves if p not in footprint]
    idx = 0

    def add(state: dict, family: str, valid: bool, note: str, target: str | None) -> None:
        nonlocal idx
        if state == good_state:
            return
        idx += 1
        out.append(_mk(state, family, valid, note, idx, "targeted", target, ph))

    # 1. omission: undo one required effect
    for p in changed:
        s = copy.deepcopy(good_state)
        before = pget(initial_state, p, _SENTINEL)
        if before is _SENTINEL:
            pdel(s, p)
        else:
            pset(s, p, copy.deepcopy(before))
        add(s, "omission", False, f"reverted required effect at {p}", p)

    # 2. partial completion: undo a proper subset of >= 2 effects (size 1 would be
    #    indistinguishable from the omission family and is deduplicated away).
    if len(changed) >= 3:
        for k in sorted({2, len(changed) - 1}):
            if not 2 <= k < len(changed):
                continue
            subset = changed[:k]
            s = copy.deepcopy(good_state)
            for p in subset:
                before = pget(initial_state, p, _SENTINEL)
                pdel(s, p) if before is _SENTINEL else pset(s, p, copy.deepcopy(before))
            add(s, "partial_completion", False,
                f"only {len(changed)-k}/{len(changed)} required effects applied", subset[0])

    # 3/4. wrong value and near miss on each produced value
    for p in changed:
        after = pget(good_state, p, _SENTINEL)
        if after is _SENTINEL:
            continue
        for near, fam in ((False, "wrong_value"), (True, "near_miss")):
            if _is_free_text(after):
                # Only perturb literals the instruction actually names; arbitrary
                # rewording of a notification body is not a semantic regression.
                new = _perturb_free_text(after, tokens, rng, near)
                if new is None:
                    continue
            else:
                new = _perturb_value(copy.deepcopy(after), rng, near)
            s = copy.deepcopy(good_state)
            pset(s, p, new)
            add(s, fam, False, f"{'near-miss' if near else 'wrong'} value at {p}", p)

    # 5. wrong entity: same effect applied to a sibling resource
    for p in changed:
        after = pget(good_state, p, _SENTINEL)
        if after is _SENTINEL:
            continue
        before = pget(initial_state, p, _SENTINEL)
        parent = parent_of(p)
        grandparent = parent_of(parent)
        # (a) sibling key inside the same container
        pnode = pget(good_state, parent, _SENTINEL)
        if isinstance(pnode, dict):
            sibs = [k for k in pnode if f"{parent}/{_esc(k)}" not in footprint]
            rng.shuffle(sibs)
            for k in sibs[:1]:
                s = copy.deepcopy(good_state)
                pdel(s, p) if before is _SENTINEL else pset(s, p, copy.deepcopy(before))
                pset(s, f"{parent}/{_esc(k)}", copy.deepcopy(after))
                add(s, "wrong_entity", False, f"effect applied to sibling key {k} instead of {p}", p)
        # (b) sibling container under the same grandparent (wrong recipient / wrong directory)
        gnode = pget(good_state, grandparent, _SENTINEL) if grandparent else _SENTINEL
        leafkey = p.split("/")[-1]
        if isinstance(gnode, dict):
            conts = [k for k in gnode if f"{grandparent}/{_esc(k)}" != parent
                     and isinstance(gnode[k], type(pget(good_state, parent)))]
            rng.shuffle(conts)
            for k in conts[:1]:
                s = copy.deepcopy(good_state)
                pdel(s, p) if before is _SENTINEL else pset(s, p, copy.deepcopy(before))
                tgt = f"{grandparent}/{_esc(k)}/{leafkey}"
                if pset(s, tgt, copy.deepcopy(after)):
                    add(s, "wrong_entity", False, f"effect redirected to {tgt}", p)

    # 6. duplicate effect
    for p in changed:
        after = pget(good_state, p, _SENTINEL)
        before = pget(initial_state, p, _SENTINEL)
        s = copy.deepcopy(good_state)
        if isinstance(after, list) and after:
            pset(s, p, list(after) + [copy.deepcopy(after[-1])])
            add(s, "duplicate_effect", False, f"repeated the last appended effect at {p}", p)
        elif before is _SENTINEL and after is not _SENTINEL:
            dup = f"{parent_of(p)}/{p.split('/')[-1]}-copy"
            if pset(s, dup, copy.deepcopy(after)):
                add(s, "duplicate_effect", False, f"created a duplicate object at {dup}", p)

    # 7/8. collateral deletion and modification of out-of-scope state
    objs = [o for o in object_paths(good_state)
            if o and not any(f == o or f.startswith(o + "/") for f in footprint)]
    rng.shuffle(objs)
    for o in objs[:2]:
        s = copy.deepcopy(good_state)
        if pdel(s, o):
            add(s, "collateral_deletion", False, f"deleted unrelated object {o}", o)
    rng.shuffle(outside)
    for p in outside[:2]:
        s = copy.deepcopy(good_state)
        pset(s, p, _perturb_value(copy.deepcopy(pget(good_state, p)), rng, False))
        add(s, "collateral_modification", False, f"modified unrelated leaf {p}", p)

    # 9. extra creation inside an in-scope container
    touched_containers = sorted({parent_of(p) for p in changed if parent_of(p)})
    for c in touched_containers[:2]:
        node = pget(good_state, c, _SENTINEL)
        if isinstance(node, dict) and node:
            proto = copy.deepcopy(next(iter(node.values())))
            s = copy.deepcopy(good_state)
            if pset(s, f"{c}/unauthorized-extra", proto):
                add(s, "extra_creation", False, f"unauthorized extra object created in {c}", c)

    # 10. state staleness: effect applied to an out-of-date twin resource
    for p in changed[:3]:
        after = pget(good_state, p, _SENTINEL)
        before = pget(initial_state, p, _SENTINEL)
        grandparent = parent_of(parent_of(p))
        gnode = pget(good_state, grandparent, _SENTINEL) if grandparent else _SENTINEL
        if after is _SENTINEL or not isinstance(gnode, dict):
            continue
        stale = [f"{grandparent}/{_esc(k)}/{_esc(kk)}"
                 for k, v in gnode.items() if isinstance(v, dict)
                 for kk in v
                 if f"{grandparent}/{_esc(k)}/{_esc(kk)}" not in footprint]
        rng.shuffle(stale)
        for tgt in stale[:1]:
            s = copy.deepcopy(good_state)
            pdel(s, p) if before is _SENTINEL else pset(s, p, copy.deepcopy(before))
            pset(s, tgt, copy.deepcopy(after))
            add(s, "state_staleness", False, f"effect applied to stale resource {tgt}", p)

    # 11. scope escape: a root the task should never touch
    footprint_roots = {p.split("/")[1] for p in footprint if len(p.split("/")) > 1}
    for root in sorted(set(good_state) - footprint_roots):
        cands = [p for p in leaf_paths(good_state) if p.startswith(f"/{root}/")]
        if not cands:
            continue
        tgt = rng.choice(cands)
        s = copy.deepcopy(good_state)
        pset(s, tgt, _perturb_value(copy.deepcopy(pget(good_state, tgt)), rng, False))
        add(s, "scope_escape", False, f"touched out-of-scope root /{root} at {tgt}", tgt)

    # 12. forbidden transition: destroy the object the task was supposed to update
    for p in changed:
        parent = parent_of(p)
        if pget(initial_state, parent, _SENTINEL) is _SENTINEL or parent.count("/") < 2:
            continue
        s = copy.deepcopy(good_state)
        if pdel(s, parent):
            add(s, "forbidden_transition", False, f"destroyed {parent} instead of updating it", parent)
            break

    return _dedupe_and_budget(out, budget, rng)


def generate_random_mutants(
    good_state: dict[str, Any], rng: random.Random, budget: int = 16
) -> list[StateMutant]:
    """Task-unaware perturbations: the ablation baseline for targeted mutation."""
    ph = state_hash(good_state)
    out: list[StateMutant] = []
    leaves = leaf_paths(good_state)
    objs = [o for o in object_paths(good_state) if o.count("/") >= 2]
    idx = 0
    attempts = 0
    while len(out) < budget and attempts < budget * 8:
        attempts += 1
        choice = rng.random()
        s = copy.deepcopy(good_state)
        if choice < 0.55 and leaves:
            p = rng.choice(leaves)
            pset(s, p, _perturb_value(copy.deepcopy(pget(good_state, p)), rng, rng.random() < 0.5))
            fam, note, tgt = "random_modify", f"random value change at {p}", p
        elif choice < 0.85 and objs:
            p = rng.choice(objs)
            if not pdel(s, p):
                continue
            fam, note, tgt = "random_delete", f"random object deletion at {p}", p
        elif objs:
            p = rng.choice(objs)
            node = pget(good_state, p)
            proto = copy.deepcopy(next(iter(node.values()))) if isinstance(node, dict) and node else "x"
            if not pset(s, f"{p}/random-extra-{idx}", proto):
                continue
            fam, note, tgt = "random_create", f"random object creation in {p}", p
        else:
            continue
        if s == good_state:
            continue
        idx += 1
        out.append(_mk(s, fam, False, note, idx, "random", tgt, ph))
    return _dedupe_and_budget(out, budget, rng)


def generate_valid_variations(
    good_state: dict[str, Any],
    extra_valid_states: list[dict[str, Any]],
    rng: random.Random,
    budget: int = 6,
) -> list[StateMutant]:
    """States a correct contract must ACCEPT: the false-reject probe set."""
    ph = state_hash(good_state)
    out: list[StateMutant] = []
    idx = 0
    for st in extra_valid_states:
        if st == good_state:
            continue
        idx += 1
        out.append(_mk(copy.deepcopy(st), "alternate_valid_trajectory", True,
                       "alternative correct execution of the same task", idx, "valid", None, ph))

    # Order-independent re-orderings of message-like lists.
    for p in leaf_paths(good_state):
        v = pget(good_state, p)
        if isinstance(v, list) and len(v) > 1:
            s = copy.deepcopy(good_state)
            pset(s, p, list(reversed(v)))
            idx += 1
            out.append(_mk(s, "reordering", True, f"order-independent list {p} reordered",
                           idx, "valid", p, ph))

    # Equivalent phrasing: a correct agent may word its messages differently.
    for p in leaf_paths(good_state):
        v = pget(good_state, p)
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            s = copy.deepcopy(good_state)
            pset(s, p, [f"{x} (sent automatically)" for x in v])
            idx += 1
            out.append(_mk(s, "equivalent_phrasing", True,
                           f"same effect, different wording at {p}", idx, "valid", p, ph))
    return _dedupe_and_budget(out, budget, rng)


def _dedupe_and_budget(
    mutants: list[StateMutant], budget: int, rng: random.Random
) -> list[StateMutant]:
    """Deduplicate by state hash, then sample within budget with family balance."""
    seen: set[str] = set()
    uniq: list[StateMutant] = []
    for m in mutants:
        h = state_hash(m.state)
        if h in seen:
            continue
        seen.add(h)
        uniq.append(m)
    if len(uniq) <= budget:
        return uniq
    by_family: dict[str, list[StateMutant]] = {}
    for m in uniq:
        by_family.setdefault(m.family, []).append(m)
    for lst in by_family.values():
        rng.shuffle(lst)
    picked: list[StateMutant] = []
    families = sorted(by_family)
    while len(picked) < budget and any(by_family[f] for f in families):
        for f in families:
            if by_family[f] and len(picked) < budget:
                picked.append(by_family[f].pop())
    return picked


# --------------------------------------------------------- legacy toy interface
def toy_mutants(good_state: dict[str, Any]) -> list[StateMutant]:
    """The original four hand-written ToyWorld mutants (kept for scripts/smoke_test.py)."""
    out = []
    s = copy.deepcopy(good_state)
    s["messages"]["alice"] = []
    out.append(StateMutant("toy-omit-message", "omission", False, s))

    s = copy.deepcopy(good_state)
    msg = s["messages"]["alice"].pop() if s["messages"]["alice"] else "report archived"
    s["messages"]["bob"].append(msg)
    out.append(StateMutant("toy-wrong-recipient", "wrong_entity", False, s))

    s = copy.deepcopy(good_state)
    s["files"]["inbox"].pop("report-draft.pdf", None)
    out.append(StateMutant("toy-collateral-delete", "collateral_deletion", False, s))

    s = copy.deepcopy(good_state)
    s["messages"]["alice"].append("duplicate")
    out.append(StateMutant("toy-duplicate", "duplicate_effect", False, s))
    return out
