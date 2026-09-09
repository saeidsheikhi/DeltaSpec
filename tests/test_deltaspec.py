"""DeltaSpec: mining, assembly, synthetic mutants, label grammar — offline and deterministic."""
from __future__ import annotations

import copy
import json

from deltaspec.assemble import assemble
from deltaspec.label import label_grammar, render_menu
from deltaspec.mine import mine_facts, scope_predicate
from deltaspec.mutants import build_mutants
from effectgate.contracts.evaluator import evaluate_contract
from effectgate.contracts.io import contract_from_dict
from effectgate.contracts.schema import lint_contract
from test_linker import S0, S1, DELTA


def _c(d):
    return contract_from_dict(d)


def test_every_mined_fact_holds_on_the_reference_by_construction():
    pre, post = S0(), S1()
    facts = mine_facts(pre, post, DELTA)
    assert facts
    for f in facts:
        c = {"contract_version": "0.5", "task_id": "t", "required": f.predicates if f.clause == "required" else [],
             "forbidden": f.predicates if f.clause == "forbidden" else [], "invariants": [], "alternatives": [], "assumptions": []}
        r = evaluate_contract(_c(c), pre, post)
        assert r.passed, (f.fid, f.kind, f.text, r.to_dict())
        # lint needs at least one required predicate; a prohibition-only contract is
        # rejected by rule, so lint prohibitions alongside a trivially-true required one
        if f.clause == "forbidden":
            c["required"] = [{"kind": "delta", "path": "/records/spotify/SongLike", "op": "added_count_ge",
                              "value": 1, "description": "", "critical": True}]
        assert lint_contract(_c(c), pre).ok, (f.fid, lint_contract(_c(c), pre).errors)


def test_mining_finds_table_field_relation_and_update_facts():
    facts = mine_facts(S0(), S1(), DELTA)
    kinds = {(f.kind, f.table) for f in facts}
    assert ("table_added", "spotify.SongLike") in kinds
    assert ("table_updated", "spotify.MusicPlayer") in kinds
    # every added SongLike row's song_id is a song in my playlists (relation via PlaylistSong)
    rel = [f for f in facts if f.kind == "added_relation" and f.table == "spotify.SongLike"]
    assert rel and rel[0].detail["column"] == "song_id"
    # the MusicPlayer update: the discriminating selector is the constant `is_playing` -> no; id-free
    upd = [f for f in facts if f.kind in ("updated_selector_field", "updated_field") and f.table == "spotify.MusicPlayer"]
    assert upd
    # prohibitions exist for changed tables and their FK parents, and never include user tables
    pro = [f for f in facts if f.kind == "prohibition"]
    assert pro and all(not f.table.endswith(".User") for f in pro)


def test_identity_and_timestamp_fields_are_never_mined_as_values():
    facts = mine_facts(S0(), S1(), DELTA)
    for f in facts:
        if f.kind == "added_field":
            assert f.detail["field"] not in ("id", "user_id", "created_at", "record_hash")


def test_mining_is_deterministic():
    a = [f.to_dict() for f in mine_facts(S0(), S1(), DELTA)]
    s0 = json.loads(json.dumps(S0())); s1 = json.loads(json.dumps(S1()))
    s0["fields"]["spotify"] = dict(reversed(list(s0["fields"]["spotify"].items())))
    b = [f.to_dict() for f in mine_facts(s0, s1, DELTA)]
    assert a == b


def test_assembly_uses_only_required_facts_and_abstains_without_an_effect():
    facts = mine_facts(S0(), S1(), DELTA)
    tbl = next(f for f in facts if f.kind == "table_added" and f.table == "spotify.SongLike")
    rel = next(f for f in facts if f.kind == "added_relation" and f.table == "spotify.SongLike")
    pro = next(f for f in facts if f.kind == "prohibition" and f.table == "spotify.SongLike")
    labels = {f.fid: "incidental" for f in facts}
    labels.update({tbl.fid: "required", rel.fid: "required", pro.fid: "required"})
    a = assemble("t", facts, labels, DELTA)
    assert a.status == "ok" and a.n_required == 3 and a.n_forbidden == 1
    c = _c(a.contract)
    assert evaluate_contract(c, S0(), S1()).passed and lint_contract(c, S0()).ok
    assert [p["kind"] for p in a.contract["invariants"]] == ["scope"]
    # nothing required and nothing side_effect -> abstain, never an accept-everything contract
    # (all-side_effect is handled by the orphan rule; see its own test)
    a2 = assemble("t", facts, {f.fid: "incidental" for f in facts}, DELTA)
    assert a2.status == "abstain" and a2.contract is None
    # a detail fact without its parent table effect does not count as an effect
    a3 = assemble("t", facts, {rel.fid: "required"}, DELTA)
    assert a3.status == "abstain"


def test_assembled_contract_rejects_regressions_and_tolerates_the_reference():
    pre, post = S0(), S1()
    facts = mine_facts(pre, post, DELTA)
    labels = {f.fid: "required" for f in facts if f.kind in ("table_added", "added_relation", "table_updated", "prohibition")}
    labels.update({f.fid: "side_effect" for f in facts if f.fid not in labels})
    a = assemble("t", facts, labels, DELTA)
    c = _c(a.contract)
    assert evaluate_contract(c, pre, post).passed
    muts = build_mutants(pre, post, DELTA)
    names = {m["name"] for m in muts}
    assert any(n.startswith("missing_effect:spotify.SongLike") for n in names)
    assert any(n.startswith("extra_row:") for n in names) and any(n.startswith("collateral_delete:") for n in names)
    killed = {m["name"]: not evaluate_contract(c, pre, m["state"]).passed for m in muts}
    assert killed[next(n for n in names if n.startswith("missing_effect:spotify.SongLike"))]
    assert all(v for k, v in killed.items() if k.startswith(("extra_row:", "collateral_delete:")))


def test_mutants_are_consistent_across_tiers_and_deterministic():
    pre, post = S0(), S1()
    m = build_mutants(pre, post, DELTA)
    for x in m:
        st = x["state"]
        # tier 1 is an owner-scoped SUBSET of tier 0 by design; a mutant must keep it so
        for app, ts in st["fields"].items():
            for t, v in ts.items():
                assert set(v["rows"]) <= set(st["records"][app][t]), (x["name"], app, t)
    again = build_mutants(json.loads(json.dumps(pre)), json.loads(json.dumps(post)), DELTA)
    assert [x["name"] for x in m] == [x["name"] for x in again]
    assert post == S1()   # inputs untouched


def test_label_grammar_enumerates_fact_ids_and_labels_only():
    facts = mine_facts(S0(), S1(), DELTA)
    g = label_grammar([f.fid for f in facts])
    item = g["properties"]["labels"]["items"]["properties"]
    assert set(item["id"]["enum"]) == {f.fid for f in facts}
    assert set(item["label"]["enum"]) == {"required", "side_effect", "incidental", "unsure"}
    menu = render_menu(facts)
    assert all(f.fid + "." in menu for f in facts) and "PROHIBITION" in menu


def test_scope_predicate_tolerates_every_reference_change_and_nothing_else():
    c = {"contract_version": "0.5", "task_id": "t", "required": [], "forbidden": [],
         "invariants": [scope_predicate(DELTA)], "alternatives": [], "assumptions": []}
    assert evaluate_contract(_c(c), S0(), S1()).passed
    bad = S1(); bad["records"]["spotify"]["Playlist"]["1"] = "x"
    assert not evaluate_contract(_c(c), S0(), bad).passed


def test_exact_count_fact_is_offered_and_requires_its_parent_effect():
    """'all X' tasks: the count follows from the pre-state, so exactness is legitimate; but it
    is only an effect if the table-level effect itself is required."""
    facts = mine_facts(S0(), S1(), DELTA)
    ex = next(f for f in facts if f.kind == "added_count_exact" and f.table == "spotify.SongLike")
    assert ex.predicates[0]["op"] == "added_count_eq" and ex.predicates[0]["value"] == 3
    tbl = next(f for f in facts if f.kind == "table_added" and f.table == "spotify.SongLike")
    a = assemble("t", facts, {tbl.fid: "required", ex.fid: "required"}, DELTA)
    assert a.status == "ok"
    c = _c(a.contract)
    assert evaluate_contract(c, S0(), S1()).passed
    # a partial completion (2 of 3 likes) is now rejected
    partial = S1(); del partial["fields"]["spotify"]["SongLike"]["rows"]["503"]; del partial["records"]["spotify"]["SongLike"]["503"]
    assert not evaluate_contract(c, S0(), partial).passed
    # exact without its parent effect does not count as an effect
    assert assemble("t", facts, {ex.fid: "required"}, DELTA).status == "abstain"


def test_mining_handles_list_valued_changed_fields():
    """MusicPlayer.queue_song_ids changed from [] to [100]: unhashable, no scalar transition,
    but the update fact must still be mined (dev crash on b0a8eae_1)."""
    facts = mine_facts(S0(), S1(), DELTA)
    mp = [f for f in facts if f.table == "spotify.MusicPlayer"]
    assert any(f.kind in ("updated_selector_field", "updated_field") for f in mp)
    for f in mp:
        for p in f.predicates:
            if p["op"] == "field_transition":
                assert not isinstance(p["value"]["to"], list)


def test_all_rows_updated_needs_no_selector_and_list_fields_get_no_transition():
    facts = mine_facts(S0(), S1(), DELTA)
    mp = [f for f in facts if f.table == "spotify.MusicPlayer" and f.kind == "updated_selector_field"]
    assert mp, "MusicPlayer update facts expected"
    for f in mp:
        assert f.detail.get("selected_by") is None          # the only row was updated
        for p in f.predicates:
            assert p["where"] is None
            if p["field"] == "queue_song_ids":
                assert p["op"] == "field_changed"           # list field: no transition
            if p["field"] == "is_playing":
                assert p["op"] == "field_transition" and p["value"] == {"from": False, "to": True}


def test_orphan_side_effect_labels_are_promoted_when_nothing_is_required():
    """A side effect is a consequence of a required effect; with nothing required, the
    reference's own table-level changes are the task (4fab96f_1: 7 Notification rows)."""
    facts = mine_facts(S0(), S1(), DELTA)
    labels = {f.fid: ("side_effect" if f.kind in ("table_added", "table_updated") else "incidental") for f in facts}
    a = assemble("t", facts, labels, DELTA)
    assert a.status == "ok" and any(n.startswith("A.orphan_side_effect") for n in a.notes)
    assert a.n_required >= 1
    # but incidental/unsure table effects are NOT promoted: still abstain
    a2 = assemble("t", facts, {f.fid: "incidental" for f in facts}, DELTA)
    assert a2.status == "abstain"
