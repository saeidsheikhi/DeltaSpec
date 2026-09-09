"""Tier-aware analysis of the frozen DeltaSpec validation run (reporting only; reads recorded rows)."""
import json, sys, collections
from pathlib import Path
sys.path.insert(0, "src")
from effectgate.analysis.stats import wilson
sel = json.load(open("results/deltaspec_fresh/VALIDATION_SET_FROZEN.json"))
A = {t["task_id"] for t in sel["tier_a"]["tasks"]}; B = {t["task_id"] for t in sel["tier_b"]["tasks"]}
conds = sys.argv[1].split(",") if len(sys.argv) > 1 else ["deltaspec_fresh"]
rows = {}
for l in Path("results/deltaspec_fresh/runs.jsonl").read_text().splitlines():
    if not l.strip(): continue
    r = json.loads(l)
    if r["condition"] in conds:
        rows[r["task_id"]] = r          # later conditions in the list override earlier ones (primary + retry)
oneshot = {}
for p in (Path("results/deltaspec_fresh/oneshot_on_variants.jsonl"), Path("results/deltaspec_fresh/oneshot_on_variants_rof.jsonl")):
    if p.exists() and (p.name == "oneshot_on_variants.jsonl" or "deltaspec_fresh_rof" in conds):
        for l in p.read_text().splitlines():
            if l.strip():
                o = json.loads(l); oneshot[o["task_id"]] = o
def w(k, d): return f"{k}/{d} = {wilson(k,d).p:.2f} [{wilson(k,d).lo:.2f},{wilson(k,d).hi:.2f}]" if d else "–"
def classify(r):
    if r.get("official_reference") is False: return "invalid_reference(harness)"
    if r.get("status") == "ok": return "auto_accept" if r.get("accepts_reference") else "incorrect_block"
    if "ConnectionError" in (r.get("error") or r.get("reason") or ""): return "abstain(transport)"
    return "abstain"
for name, ids in (("Tier A (scenario-fresh)", A), ("Tier B (task-fresh)", B), ("A ∪ B", A | B)):
    rs = [rows[t] for t in sorted(ids) if t in rows]
    cls = collections.Counter(classify(r) for r in rs)
    ev = [r for r in rs if classify(r) != "invalid_reference(harness)"]
    ok = [r for r in ev if classify(r) == "auto_accept"]
    neg = sum(len([v for v, o in (r.get("variant_official") or {}).items() if o is False]) for r in ok)
    pos = sum(len([v for v, o in (r.get("variant_official") or {}).items() if o is True and v != "reference"]) for r in ok)
    unsafe = sum(len(r.get("unsafe_promotions") or []) for r in ok); killed = sum(len(r.get("killed_variants") or []) for r in ok)
    fb = sum(len(r.get("false_block_variants") or []) for r in ok)
    sk = sum(sum((r.get("synthetic_mutants") or {}).values()) for r in ok); st = sum(len(r.get("synthetic_mutants") or {}) for r in ok)
    hu = sum(len((r.get("hash_baseline") or {}).get("unsafe", [])) for r in ok); hf = sum(len((r.get("hash_baseline") or {}).get("false_block", [])) for r in ok)
    ou = sum(len((r.get("output_baseline") or {}).get("unsafe", [])) for r in ok); of = sum(len((r.get("output_baseline") or {}).get("false_block", [])) for r in ok)
    tasks_unsafe = [r["task_id"] for r in ok if r.get("unsafe_promotions")]; tasks_fb = [r["task_id"] for r in ok if r.get("false_block_variants")]
    print(f"\n## {name}: {len(rs)} rows, evaluable {len(ev)}; classes {dict(cls)}")
    print(f"- auto-accept known-good: {w(len(ok), len(ev))} (of evaluable); {w(len(ok), len(rs))} of all rows")
    print(f"- incorrect blocks {cls.get('incorrect_block',0)}; abstain {cls.get('abstain',0)+cls.get('abstain(transport)',0)} (transport {cls.get('abstain(transport)',0)})")
    print(f"- unsafe promotions on officially-failed variants: {unsafe}/{neg}  tasks {tasks_unsafe}")
    print(f"- variant kill rate: {w(killed, neg)}")
    print(f"- false blocks on officially-passed variants: {fb}/{pos}  tasks {tasks_fb}")
    print(f"- synthetic mutant kill rate: {w(sk, st)}")
    print(f"- state-hash baseline: unsafe {hu}/{neg}, false blocks {hf}/{pos}; output-text baseline: unsafe {ou}/{neg}, false blocks {of}/{pos}")
    os_ = [oneshot[t] for t in sorted(ids) if t in oneshot and oneshot[t].get("status") == "ok"]
    if os_:
        on = sum(len([v for v, o in o["variant_official"].items() if o is False]) for o in os_)
        op = sum(len([v for v, o in o["variant_official"].items() if o is True and v != "reference"]) for o in os_)
        print(f"- v0.6.1 one-shot on same variants ({len(os_)} tasks with a contract): accepts ref {sum(bool(o['accepts_reference']) for o in os_)}/{len(os_)}, "
              f"unsafe {sum(len(o['unsafe']) for o in os_)}/{on}, false blocks {sum(len(o['false_block']) for o in os_)}/{op}")
        # like-for-like: DeltaSpec on the same tasks
        same = [rows[o["task_id"]] for o in os_ if o["task_id"] in rows and classify(rows[o["task_id"]]) == "auto_accept"]
        print(f"  DeltaSpec on those same tasks: unsafe {sum(len(r.get('unsafe_promotions') or []) for r in same)}, false blocks {sum(len(r.get('false_block_variants') or []) for r in same)}")
    # per scenario
    per = collections.defaultdict(list)
    for r in rs: per[r["task_id"].rsplit("_",1)[0]].append(r)
    for sc, rr in sorted(per.items()):
        print(f"    {sc}: " + "; ".join(f"{r['task_id']} {classify(r)} unsafe={len(r.get('unsafe_promotions') or [])} fb={len(r.get('false_block_variants') or [])}" for r in rr))
