"""Paired statistics for method comparisons (STATISTICS_PLAN.md).

* McNemar's exact test for paired binary disagreement on the same cases.
* Cluster (task-level) bootstrap confidence intervals for rate differences, because
  many mutants/cases are nested inside the same original task and are not independent.
* Wilson intervals for headline proportions.
* Holm correction over a pre-declared family of comparisons.

Only ``scipy``/``numpy`` are used, and every function is deterministic given a seed.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


@dataclass
class Proportion:
    k: int
    n: int
    lo: float
    hi: float

    @property
    def p(self) -> float:
        return self.k / self.n if self.n else float("nan")

    def to_dict(self) -> dict[str, Any]:
        return {"k": self.k, "n": self.n, "p": self.p, "ci95": [self.lo, self.hi]}

    def __str__(self) -> str:
        return f"{self.p:.3f} [{self.lo:.3f}, {self.hi:.3f}] (n={self.n})"


def wilson(k: int, n: int, z: float = 1.959963984540054) -> Proportion:
    """Wilson score interval: well-behaved at p near 0 or 1, unlike the normal interval."""
    if n == 0:
        return Proportion(0, 0, float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return Proportion(k, n, max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class McNemarResult:
    b: int          # method A correct, method B wrong
    c: int          # method A wrong, method B correct
    p_value: float
    n_discordant: int

    def to_dict(self) -> dict[str, Any]:
        return {"b": self.b, "c": self.c, "n_discordant": self.n_discordant,
                "p_value": self.p_value}


def mcnemar(a: Sequence[bool], b: Sequence[bool]) -> McNemarResult:
    """Exact two-sided McNemar test on paired binary outcomes.

    ``a[i]``/``b[i]`` are the two methods' verdicts on the *same* case i.
    """
    if len(a) != len(b):
        raise ValueError("paired sequences must have equal length")
    n01 = sum(1 for x, y in zip(a, b) if x and not y)
    n10 = sum(1 for x, y in zip(a, b) if y and not x)
    n = n01 + n10
    if n == 0:
        return McNemarResult(n01, n10, 1.0, 0)
    # Exact binomial test with p = 0.5, two-sided.
    k = min(n01, n10)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return McNemarResult(n01, n10, min(1.0, 2 * tail), n)


def cluster_bootstrap_diff(
    clusters: dict[str, list[tuple[bool, bool]]],
    n_boot: int = 10000,
    seed: int = 42,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Bootstrap CI for the difference in success rate between two paired methods.

    ``clusters`` maps a cluster id (the original task) to the list of paired
    ``(method_a, method_b)`` outcomes inside it. Resampling is done over *clusters*,
    not over individual cases, so mutants nested in one task are not treated as
    independent observations.
    """
    keys = list(clusters)
    if not keys:
        return {"diff": float("nan"), "ci95": [float("nan")] * 2, "n_clusters": 0, "n_cases": 0}

    def rate_diff(sel: Iterable[str]) -> float:
        na = nb = tot = 0
        for k in sel:
            for x, y in clusters[k]:
                na += int(x)
                nb += int(y)
                tot += 1
        return (na - nb) / tot if tot else float("nan")

    point = rate_diff(keys)
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        sample = [keys[rng.randrange(len(keys))] for _ in keys]
        d = rate_diff(sample)
        if not math.isnan(d):
            draws.append(d)
    draws.sort()
    lo = draws[int((alpha / 2) * len(draws))] if draws else float("nan")
    hi = draws[min(len(draws) - 1, int((1 - alpha / 2) * len(draws)))] if draws else float("nan")
    return {
        "diff": point, "ci95": [lo, hi],
        "n_clusters": len(keys),
        "n_cases": sum(len(v) for v in clusters.values()),
        "n_boot": len(draws),
    }


def holm(p_values: dict[str, float], alpha: float = 0.05) -> dict[str, dict[str, Any]]:
    """Holm-Bonferroni step-down correction over a pre-declared comparison family."""
    items = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, dict[str, Any]] = {}
    prev = 0.0
    for i, (name, p) in enumerate(items):
        adj = min(1.0, max(prev, (m - i) * p))
        prev = adj
        out[name] = {"p_raw": p, "p_holm": adj, "significant_at_0.05": adj < alpha}
    return out


def paired_binary_comparison(
    cases: list[dict[str, Any]],
    method_a: str,
    method_b: str,
    outcome: str = "correct",
    cluster_key: str = "task_id",
    seed: int = 42,
) -> dict[str, Any]:
    """Full paired comparison of two methods over the same cases."""
    a = [bool(c[method_a]) for c in cases]
    b = [bool(c[method_b]) for c in cases]
    clusters: dict[str, list[tuple[bool, bool]]] = {}
    for c in cases:
        clusters.setdefault(str(c.get(cluster_key, "_")), []).append((bool(c[method_a]),
                                                                      bool(c[method_b])))
    mc = mcnemar(a, b)
    boot = cluster_bootstrap_diff(clusters, seed=seed)
    return {
        "method_a": method_a, "method_b": method_b, "outcome": outcome,
        "n_cases": len(cases),
        "rate_a": wilson(sum(a), len(a)).to_dict(),
        "rate_b": wilson(sum(b), len(b)).to_dict(),
        "mcnemar": mc.to_dict(),
        "cluster_bootstrap": boot,
    }
