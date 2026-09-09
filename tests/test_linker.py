"""v0.6 deterministic contract linker — one regression test per rule.

Fixture: the 'songs in my playlists' world from test_relational_v05, plus a tier-0-only
table (`venmo.Notification` rows that belong to other users) and a known-good transition.
Every test asserts on the transformation log, so a rule that silently stops firing fails.
"""
from __future__ import annotations

import copy
import json

from effectgate.contracts.evaluator import evaluate_contract
from effectgate.contracts.io import contract_from_dict
from effectgate.contracts.linker import _derive_path, link_contract
from effectgate.contracts.schema import ollama_format_schema
from effectgate.contracts.compiler import schema_vocab


def _view(rows, types, fks=None, scope="owner"):
    return {"scope": scope, "n_total": len(rows), "n_projected": len(rows), "truncated": False,
            "field_types": types, "primary_key": "id", "foreign_keys": fks or {}, "rows": rows}


def S0():
    return {
        "counts": {"spotify": {"Playlist": 2, "PlaylistSong": 3, "Song": 4, "SongLike": 1,
                               "UserArtistFollowing": 1, "MusicPlayer": 1},
                   "venmo": {"Notification": 3}},
        "records": {"spotify": {"Playlist": {"1": "p", "2": "p"}, "PlaylistSong": {"11": "a", "12": "b", "13": "c"},
                                "Song": {"100": "s", "101": "s", "102": "s", "103": "s"}, "SongLike": {"500": "l"},
                                "UserArtistFollowing": {"900": "f"}, "MusicPlayer": {"7": "m"}},
                    "venmo": {"Notification": {"1": "n", "2": "n", "3": "n"}}},
        "fields": {"spotify": {
            "Playlist": _view({"1": {"id": 1, "user_id": 7, "title": "Gym"}, "2": {"id": 2, "user_id": 7, "title": "Chill"}},
                              {"id": "int", "user_id": "int", "title": "str"}, {"user_id": "spotify.User"}),
            "PlaylistSong": _view({"11": {"id": 11, "playlist_id": 1, "song_id": 100}, "12": {"id": 12, "playlist_id": 1, "song_id": 101},
                                   "13": {"id": 13, "playlist_id": 2, "song_id": 102}},
                                  {"id": "int", "playlist_id": "int", "song_id": "int"},
                                  {"playlist_id": "spotify.Playlist", "song_id": "spotify.Song"}, scope="owner_child"),
            "Song": _view({"100": {"id": 100, "genre": "classical", "artist_ids": [1, 2]}, "101": {"id": 101, "genre": "rock", "artist_ids": [3]},
                           "102": {"id": 102, "genre": "classical", "artist_ids": [4]}, "103": {"id": 103, "genre": "classical", "artist_ids": [5]}},
                          {"id": "int", "genre": "str", "artist_ids": "list"}, scope="fk_hop"),
            "SongLike": _view({"500": {"id": 500, "user_id": 7, "song_id": 103}},
                              {"id": "int", "user_id": "int", "song_id": "int"}, {"user_id": "spotify.User", "song_id": "spotify.Song"}),
            "UserArtistFollowing": _view({"900": {"id": 900, "user_id": 7, "artist_id": 9}},
                                         {"id": "int", "user_id": "int", "artist_id": "int"}, {"user_id": "spotify.User", "artist_id": "spotify.Artist"}),
            "MusicPlayer": _view({"7": {"id": 7, "user_id": 7, "is_playing": False, "queue_song_ids": []}},
                                 {"id": "int", "user_id": "int", "is_playing": "bool", "queue_song_ids": "list"}, {"user_id": "spotify.User"}),
        }, "venmo": {
            "Notification": _view({}, {"id": "int", "user_id": "int", "title": "str"}, {"user_id": "venmo.User"}),
        }},
    }


def S1():
    """Known-good: liked all songs in my playlists; started playing; roommates got 2 notifications."""
    s = S0()
    sl = s["fields"]["spotify"]["SongLike"]["rows"]
    for i, sid in ((501, 100), (502, 101), (503, 102)):
        sl[str(i)] = {"id": i, "user_id": 7, "song_id": sid}; s["records"]["spotify"]["SongLike"][str(i)] = "l"
    s["records"]["spotify"]["Song"]["100"] = "s'"   # like_count bumped on a parent row (tier 0 only)
    mp = s["fields"]["spotify"]["MusicPlayer"]["rows"]["7"]; mp["is_playing"] = True; mp["queue_song_ids"] = [100]
    s["records"]["spotify"]["MusicPlayer"]["7"] = "m'"
    s["records"]["venmo"]["Notification"].update({"4": "n", "5": "n"})   # other users' rows
    return s


DELTA = {"spotify.SongLike": {"n_added": 3, "n_removed": 0, "n_updated": 0},
         "spotify.Song": {"n_added": 0, "n_removed": 0, "n_updated": 1},
         "spotify.MusicPlayer": {"n_added": 0, "n_removed": 0, "n_updated": 1},
         "venmo.Notification": {"n_added": 2, "n_removed": 0, "n_updated": 0}}


def rec(op, table, where=None, field=None, value=None):
    return {"kind": "record", "table": table, "op": op, "where": where, "field": field, "value": value,
            "description": op, "critical": True, "path": ""}


def scope(*tables):
    return {"kind": "scope", "op": "changed_tables_subset", "value": list(tables), "path": "", "table": None,
            "where": None, "field": None, "description": "", "critical": True}


def contract(required=(), forbidden=(), invariants=()):
    return {"contract_version": "0.5", "task_id": "t", "required": list(required), "forbidden": list(forbidden),
            "invariants": list(invariants), "alternatives": [], "assumptions": []}


def link(c):
    return link_contract(c, S0(), S1(), DELTA, task_id="t")


def has(log, prefix):
    return any(l.startswith(prefix) for l in log)


# ------------------------------------------------------------- A: binding
def test_A_dotted_delta_paths_are_bound_to_slash_form():
    r = link(contract([{"kind": "delta", "path": "/records/spotify.SongLike", "op": "added_count_ge", "value": 1,
                        "description": "", "critical": True}]))
    assert r.status == "ok" and has(r.transformations, "A.path")
    assert r.contract["required"][0]["path"] == "/records/spotify/SongLike"


def test_A_bare_table_is_qualified_and_unknown_table_dropped():
    r = link(contract([rec("added_count_ge", "SongLike", None, value=1), rec("exists", "spotify.Nope", None)]))
    assert has(r.transformations, "A.table required[0]: qualified")
    assert has(r.transformations, "A.table required[1]: dropped predicate on unknown table")
    assert r.contract["required"][0]["table"] == "spotify.SongLike"


def test_A_owner_conditions_and_unknown_fields_are_dropped():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", {"user_id": 7, "liked": True, "song_id": {"ge": 1}}, value=1)]))
    log = r.transformations
    assert has(log, "A.owner") and has(log, "A.field")
    assert r.contract["required"][0]["where"] == {"song_id": {"ge": 1}}


def test_A_unknown_operator_and_mistyped_literal_are_dropped_not_evaluated():
    r = link(contract([rec("added_count_ge", "spotify.SongLike",
                           {"song_id": {"neq_typo": 1, "in": ["SELECT id FROM x"]}}, value=1)]))
    assert has(r.transformations, "A.op") and has(r.transformations, "A.type")
    assert r.contract["required"][0]["where"] is None and r.status == "ok"


def test_A_invalid_record_operator_and_unknown_field_predicate_dropped():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1),
                       rec("unchanged", "spotify.SongLike", None),
                       rec("field_unchanged", "spotify.SongLike", None, "liked")]))
    assert has(r.transformations, "A.op required[1]") and has(r.transformations, "A.field required[2]")
    assert len(r.contract["required"]) == 1


def test_A_in_with_a_subselection_is_bound_to_in_related():
    r = link(contract([rec("added_count_ge", "spotify.SongLike",
                           {"song_id": {"in": {"table": "spotify.PlaylistSong", "field": "song_id", "where": None}}}, value=1)]))
    assert has(r.transformations, "A.op required[0].song_id: 'in' with a sub-selection")
    assert "in_related" in r.contract["required"][0]["where"]["song_id"]


# ------------------------------------------------------------ B: FK linking
def test_B_direct_relation_with_wrong_endpoint_field_is_rebound():
    # PlaylistSong.song_id -> Song.id is declared; the model wrote field 'genre'
    r = link(contract([rec("added_count_ge", "spotify.SongLike",
                           {"song_id": {"in_related": {"table": "spotify.Song", "field": "genre", "where": {"genre": "classical"}}}}, value=1)]))
    # SongLike.song_id -> Song.id declared, so endpoint field is bound to 'id'
    assert has(r.transformations, "B.link required[0].song_id: bound relation endpoint")
    sub = r.contract["required"][0]["where"]["song_id"]["in_related"]
    assert sub["table"] == "spotify.Song" and sub["field"] == "id" and sub["where"] == {"genre": {"eq": "classical"}}


def test_B_multi_hop_path_is_derived_from_the_fk_graph():
    path = _derive_path("spotify.SongLike", "song_id", "spotify.Playlist", {
        t: v for app, ts in S0()["fields"].items() for t, v in ((f"{app}.{k}", vv) for k, vv in ts.items())})
    assert path and path[0][0] == "spotify.SongLike" and path[-1][2] == "spotify.Playlist"
    # "songs liked that are in my playlists": SongLike.song_id -> Song.id <- PlaylistSong.song_id -> Playlist
    r = link(contract([rec("added_count_ge", "spotify.SongLike",
                           {"song_id": {"in_related": {"table": "spotify.Playlist", "field": "id", "where": None}}}, value=1)]))
    assert has(r.transformations, "B.link required[0].song_id: derived")
    assert r.status == "ok" and r.accepts_known_good is True


def test_B_undeclared_relation_is_dropped_with_reason():
    # Playlist.title has no FK; no path to Song exists through it
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1),
                       rec("exists", "spotify.Playlist", {"title": {"in_related": {"table": "spotify.Song", "field": "genre", "where": None}}})]))
    assert has(r.transformations, "B.link required[1].title: dropped relation")


def test_B_list_vs_scalar_relation_operator_is_corrected():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1),
                       rec("exists", "spotify.MusicPlayer", {"queue_song_ids": {"in_related": {"table": "spotify.Song", "field": "id", "where": None}}})]))
    assert has(r.transformations, "B.op required[1].queue_song_ids: 'in_related' on list field -> 'intersects_related'")


# ------------------------------------------------------ C: simplification
def test_C_self_referential_selector_is_removed():
    r = link(contract([rec("field_transition", "spotify.MusicPlayer", {"is_playing": True}, "is_playing", {"from": False, "to": True})]))
    assert has(r.transformations, "C.self")
    assert r.status == "ok" and r.accepts_known_good is True


def test_C_vacuous_count_ge_zero_is_dropped():
    # v0.6.1: a vacuous lower bound under `required` is named as an invalid required
    # effect (rule B); under forbidden/invariants it remains the plain C.vacuous drop.
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1),
                       rec("added_count_ge", "spotify.UserArtistFollowing", None, value=0)]))
    assert has(r.transformations, "B.vacuous_required required[1]")


# ------------------------------------------------------- D: reconciliation
def test_D_required_effect_contradicted_by_known_good_is_dropped():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1),
                       rec("added_count_ge", "spotify.MusicPlayer", None, value=1)]))
    assert has(r.transformations, "D.effect required[1]: dropped added_count_ge on spotify.MusicPlayer")
    assert r.accepts_known_good is True


def test_D_unmatched_field_transition_is_weakened_to_field_changed():
    r = link(contract([rec("field_transition", "spotify.MusicPlayer", None, "queue_song_ids", {"from": [99], "to": [100]})]))
    assert has(r.transformations, "D.weaken")
    assert r.contract["required"][0]["op"] == "field_changed" and r.accepts_known_good is True


def test_D_forbidden_predicate_true_in_known_good_is_dropped_as_polarity_error():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1)],
                      forbidden=[{"kind": "delta", "path": "/records/spotify/SongLike", "op": "added_count_ge", "value": 1,
                                  "description": "", "critical": True}]))
    assert has(r.transformations, "D.reject forbidden[0]")
    assert r.contract["forbidden"] == [] and r.accepts_known_good is True


def test_D_never_strengthens_and_logs_every_change():
    c = contract([rec("added_count_ge", "spotify.SongLike", None, value=1)], invariants=[scope("spotify.SongLike")])
    before = copy.deepcopy(c)
    r = link(c)
    assert c == before                                   # input untouched
    n_req_before = len(before["required"])
    assert len(r.contract["required"]) <= n_req_before
    assert all(l.split(" ")[0][:1] in "ABCDEF" for l in r.transformations)


# ------------------------------------------------------ E: scope + tier lift
def test_E_scope_is_widened_to_known_good_changed_tables_never_narrowed():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1)], invariants=[scope("spotify.SongLike")]))
    sc = [p for p in r.contract["invariants"] if p["kind"] == "scope"]
    assert len(sc) == 1 and set(sc[0]["value"]) >= set(DELTA)
    assert has(r.transformations, "E.scope invariants: widened scope by")


def test_E_missing_scope_is_derived_from_known_good_changed_tables():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1)]))
    assert has(r.transformations, "E.scope invariants: derived scope")
    sc = [p for p in r.contract["invariants"] if p["kind"] == "scope"][0]
    assert set(sc["value"]) == set(DELTA)


def test_E_scope_still_catches_collateral_change_after_linking():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1)]))
    bad = S1(); bad["records"]["spotify"]["Playlist"]["1"] = "tampered"
    assert not evaluate_contract(contract_from_dict(r.contract), S0(), bad).passed


def test_E_effect_on_other_users_rows_is_lifted_to_tier_0():
    r = link(contract([rec("added_count_ge", "venmo.Notification", None, value=1)]))
    assert has(r.transformations, "E.lift required[0]")
    p = r.contract["required"][0]
    assert p["kind"] == "delta" and p["path"] == "/records/venmo/Notification"
    assert r.accepts_known_good is True


# ---------------------------------------------------------- F: fail closed
def test_F_contract_with_no_surviving_required_effect_fails_closed():
    r = link(contract([rec("added_count_ge", "spotify.UserArtistFollowing", None, value=1)]))
    assert r.status == "empty" and r.contract is None and has(r.transformations, "F.empty")


def test_linker_is_deterministic():
    c = contract([rec("added_count_ge", "SongLike", {"user_id": 7, "song_id": {"in": {"table": "spotify.Playlist", "field": "id"}}}, value=1),
                  rec("field_transition", "spotify.MusicPlayer", {"is_playing": True}, "is_playing", {"from": False, "to": True})],
                 invariants=[scope("spotify.SongLike")])
    a = link(c).to_dict(); a.pop("transformations")
    for _ in range(3):
        b = link(json.loads(json.dumps(c))).to_dict(); b.pop("transformations")
        assert a == b


# ------------------------------------------------- grammar vocabulary
def test_vocab_grammar_enumerates_tables_and_fields():
    v = schema_vocab(S0())
    g = ollama_format_schema(v)
    pred = g["properties"]["required"]["items"]["properties"]
    assert "spotify.SongLike" in pred["table"]["enum"] and None in pred["table"]["enum"]
    assert "queue_song_ids" in pred["field"]["enum"]
    assert "enum" not in ollama_format_schema()["properties"]["required"]["items"]["properties"]["table"]


# ------------------------------------------------------------ v0.6.1 rules
def test_A_root_record_delta_operator_on_counts_is_rebound_to_records():
    """The frozen model-axis probe: qwen3.5 located the effect and wrote it under /counts/."""
    r = link(contract([{"kind": "delta", "path": "/counts/spotify/SongLike", "op": "added_count_ge", "value": 1,
                        "description": "", "critical": True}]))
    assert has(r.transformations, "A.root required[0]")
    assert r.status == "ok" and r.contract["required"][0]["path"] == "/records/spotify/SongLike"
    assert r.accepts_known_good is True


def test_A_root_does_not_touch_count_operators_or_unknown_tables():
    # `eq` on /counts/ is a legitimate integer comparison: untouched.
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1)],
                      invariants=[{"kind": "delta", "path": "/counts/spotify/Playlist", "op": "unchanged", "value": None,
                                   "description": "", "critical": True}]))
    assert not has(r.transformations, "A.root")
    assert any(p["path"] == "/counts/spotify/Playlist" for p in r.contract["invariants"])
    # a table absent from the record tier is not rebound (it would not resolve either way)
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1),
                       {"kind": "delta", "path": "/counts/spotify/Nope", "op": "added_count_ge", "value": 1,
                        "description": "", "critical": True}]))
    assert not has(r.transformations, "A.root")


def test_B_required_ge_zero_is_an_invalid_required_effect_and_is_never_read_as_one():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=0)]))
    assert r.status == "empty"
    assert has(r.transformations, "B.vacuous_required required[0]")
    assert "vacuous lower bound of 0" in (r.error or "")
    # never silently strengthened: no surviving predicate has value 1
    assert r.contract is None


def test_B_vacuous_required_alongside_a_real_effect_is_just_removed():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1),
                       rec("removed_count_ge", "spotify.SongLike", None, value=0)]))
    assert r.status == "ok" and has(r.transformations, "B.vacuous_required required[1]")
    assert [p["op"] for p in r.contract["required"]] == ["added_count_ge"]


def test_B_forbidden_ge_zero_is_still_the_plain_vacuous_rule():
    r = link(contract([rec("added_count_ge", "spotify.SongLike", None, value=1)],
                      forbidden=[rec("removed_count_ge", "spotify.SongLike", None, value=0)]))
    assert has(r.transformations, "C.vacuous forbidden[0]") and r.status == "ok"
