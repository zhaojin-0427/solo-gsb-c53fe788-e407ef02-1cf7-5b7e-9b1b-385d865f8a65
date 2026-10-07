"""Unit tests for minimal hitting sets and witness construction."""
import pytest

from app.evaluator import Evaluator, Table, label_can_hit_minimally
from app.provenance import (
    SearchBudgetExceeded,
    minimal_deletion_sets_by_recompute,
    minimal_hitting_sets,
)


def L(table, *pk):
    return (table, pk)


def test_hitting_sets_basic():
    witnesses = {frozenset([L("t", 1), L("t", 2)]), frozenset([L("t", 2), L("t", 3)])}
    hs = minimal_hitting_sets(witnesses)
    assert frozenset([L("t", 2)]) in hs
    assert frozenset([L("t", 1), L("t", 3)]) in hs
    assert len(hs) == 2


def test_hitting_sets_nested():
    witnesses = {
        frozenset([L("t", 1), L("t", 2)]),
        frozenset([L("t", 2), L("t", 3)]),
        frozenset([L("t", 3), L("t", 4)]),
    }
    hs = minimal_hitting_sets(witnesses)
    # {2,3}, {1,3}, {2,4}
    assert len(hs) == 3
    for h in hs:
        for w in witnesses:
            assert h & w
        assert not any(o < h for o in hs)


def test_single_witness():
    w = frozenset([L("a", "x"), L("b", "y")])
    hs = minimal_hitting_sets(witnesses={w})
    assert set(hs) == {frozenset([L("a", "x")]), frozenset([L("b", "y")])}


def test_deletion_sets_by_recompute():
    pool = {L("t", 1), L("t", 2)}
    # Every non-empty set kills: the singletons are the minimal solutions.
    sols = minimal_deletion_sets_by_recompute(pool, lambda d: len(d) > 0)
    assert sols == [frozenset([L("t", 1)]), frozenset([L("t", 2)])]
    # Only pairs kill: the single pair is the unique minimal solution.
    sols = minimal_deletion_sets_by_recompute(pool, lambda d: len(d) >= 2)
    assert sols == [frozenset([L("t", 1), L("t", 2)])]
    # Non-upward-closed kills (only the exact singleton works): supersets of
    # a solution are skipped, and the solution is still reported.
    sols = minimal_deletion_sets_by_recompute(
        pool, lambda d: d == frozenset([L("t", 1)])
    )
    assert sols == [frozenset([L("t", 1)])]


def test_non_killing_set_must_not_prune_its_supersets():
    """Mixed-sign sums are non-monotone: a set that fails to kill the target
    says nothing about its supersets, so the search still has to test them."""
    pool = {L("t", 1), L("t", 2), L("t", 3)}
    # Only exact pairs kill; the singletons (and the empty set) do not.
    sols = minimal_deletion_sets_by_recompute(
        pool, lambda d: len(d) == 2
    )
    assert sols == [
        frozenset([L("t", 1), L("t", 2)]),
        frozenset([L("t", 1), L("t", 3)]),
        frozenset([L("t", 2), L("t", 3)]),
    ]
    # Every odd-sized candidate kills, even sizes do not: supersets of a
    # failing singleton (the triples) must still be enumerated.
    sols = minimal_deletion_sets_by_recompute(
        pool, lambda d: len(d) % 2 == 1
    )
    assert sols == [
        frozenset([L("t", 1)]),
        frozenset([L("t", 2)]),
        frozenset([L("t", 3)]),
    ]


def test_search_budget_pool_too_large():
    # Pools beyond the enumeration cap raise a structured budget error
    # instead of returning None (no approximate hitting-set answer).
    big = {L("t", i) for i in range(21)}
    with pytest.raises(SearchBudgetExceeded) as exc:
        minimal_deletion_sets_by_recompute(big, lambda d: True)
    assert exc.value.reason == "pool_too_large"
    assert exc.value.details["reduced_pool_size"] == 21


def test_search_budget_recompute_calls():
    # A call cap lower than the 2**n enumeration surfaces mid-search.
    big = {L("t", i) for i in range(10)}
    with pytest.raises(SearchBudgetExceeded) as exc:
        minimal_deletion_sets_by_recompute(big, lambda d: False, max_calls=5)
    assert exc.value.reason == "recompute_budget"
    assert exc.value.details["recompute_calls"] == 6


def test_label_can_hit_minimally_basic():
    # z occurs in a minimal transversal iff some incident edge is not
    # dominated by a z-free edge.
    z = L("t", 0)
    a, b, c = L("t", 1), L("t", 2), L("t", 3)
    # Edge {z,a} with no smaller z-free edge: z is usable on its own.
    assert label_can_hit_minimally(z, [frozenset({z, a})])
    # Edge {z,a} strictly contains the z-free edge {a}: z is dominated.
    assert not label_can_hit_minimally(
        z, [frozenset({z, a}), frozenset({a})]
    )
    # The 18 independent zero self-pairs: each singleton edge makes its own
    # label usable, so every zero label is independently relevant.
    edges = [frozenset({L("t", i)}) for i in range(1, 20)]
    for i in range(1, 20):
        assert label_can_hit_minimally(L("t", i), edges)
    # A shared label over two edges plus a dominated third edge: still usable
    # via the undominated {a,c} edge (the minimal transversal is {a,b}).
    assert label_can_hit_minimally(a, [frozenset({a, b}), frozenset({b}), frozenset({a, c})])
    # When every a-edge dominates an a-free edge, a is never needed.
    assert not label_can_hit_minimally(
        a,
        [frozenset({a, b}), frozenset({b}), frozenset({a, c}), frozenset({c})],
    )


def test_search_reuses_gate_outcomes():
    # Outcomes handed in via `known` are not recomputed; only the remaining
    # candidates consume the per-search call budget.
    pool = {L("t", 1), L("t", 2)}
    known = {
        frozenset(): False,
        frozenset([L("t", 1)]): True,
        frozenset([L("t", 2)]): True,
    }
    sols = minimal_deletion_sets_by_recompute(
        pool,
        lambda d: (_ for _ in ()).throw(AssertionError("should use cache")),
        max_calls=0,
        known=known,
    )
    assert sols == [frozenset([L("t", 1)]), frozenset([L("t", 2)])]
    # A cached False outcome is a genuine cache hit (not mistaken for absent).
    sols = minimal_deletion_sets_by_recompute(
        pool, lambda d: False, max_calls=5,
        known={frozenset(): False, frozenset([L("t", 1)]): False},
    )
    assert sols == []


def make_evaluator(spec):
    return Evaluator({t["table"]: Table(t) for t in spec})


def join_query():
    return {
        "type": "join",
        "left": {"type": "scan", "table": "orders"},
        "right": {"type": "scan", "table": "customers"},
        "on": [{"left": "cust", "right": "id"}],
    }


def test_join_provenance():
    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "cust", "amt"],
            "rows": [
                {"oid": 1, "cust": 10, "amt": 5},
                {"oid": 2, "cust": 10, "amt": 7},
                {"oid": 3, "cust": 20, "amt": 9},
            ],
        },
        {
            "table": "customers",
            "primary_key": ["id"],
            "columns": ["id", "name"],
            "rows": [{"id": 10, "name": "A"}, {"id": 20, "name": "B"}],
        },
    ]
    ev = make_evaluator(tables)
    rows = ev.eval(join_query())
    # find joined row oid=1
    cols = ev.schema(join_query())
    r1 = next(r for r in rows if r["data"][cols.index("oid")] == 1)
    assert r1["witnesses"] == {frozenset([L("orders", 1), L("customers", 10)])}
    assert len(rows) == 3


def test_aggregate_group_witnesses():
    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "cust", "amt"],
            "rows": [
                {"oid": 1, "cust": 10, "amt": 5},
                {"oid": 2, "cust": 10, "amt": 7},
                {"oid": 3, "cust": 20, "amt": 9},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "orders"},
        "group_by": ["cust"],
        "aggregates": [
            {"fn": "count", "column": "*", "as": "n"},
            {"fn": "sum", "column": "amt", "as": "total"},
            {"fn": "min", "column": "amt", "as": "lo"},
            {"fn": "max", "column": "amt", "as": "hi"},
        ],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    cols = ev.schema(plan)
    g10 = next(r for r in rows if r["data"][cols.index("cust")] == 10)
    # count(*)=2 covers both members; sum=12 has the single minimal combo
    # {1,2}; min=5 pins row 1 and max=7 pins row 2. The conjunction of all
    # components requires both rows.
    assert g10["witnesses"] == {frozenset([L("orders", 1), L("orders", 2)])}
    data = dict(zip(cols, g10["data"]))
    assert data == {"cust": 10, "n": 2, "total": 12, "lo": 5, "hi": 7}

    # Deleting either member changes the aggregate values, so each singleton
    # is a minimal deletion set — matching recomputation.
    hs = minimal_hitting_sets(g10["witnesses"])
    assert hs == [frozenset([L("orders", 1)]), frozenset([L("orders", 2)])]


def test_aggregate_min_max_witnesses():
    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "amt"],
            "rows": [
                {"oid": 1, "amt": 50},
                {"oid": 2, "amt": 30},
                {"oid": 3, "amt": 30},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "orders"},
        "aggregates": [{"fn": "min", "column": "amt", "as": "lo"}],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    # min=30: each equal-valued row is a minimal combo on its own.
    assert rows[0]["witnesses"] == {
        frozenset([L("orders", 2)]),
        frozenset([L("orders", 3)]),
    }
    hs = minimal_hitting_sets(rows[0]["witnesses"])
    assert hs == [frozenset([L("orders", 2), L("orders", 3)])]


def test_aggregate_sum_minimal_combos():
    from app.evaluator import minimal_sum_combos

    tables = [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": ["oid", "amt"],
            "rows": [
                {"oid": 1, "amt": 50},
                {"oid": 2, "amt": 30},
                {"oid": 3, "amt": 0},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "orders"},
        "aggregates": [{"fn": "sum", "column": "amt", "as": "total"}],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    # The zero-valued row never participates in a minimal sum combination.
    assert rows[0]["data"] == (80,)
    assert rows[0]["witnesses"] == {frozenset([L("orders", 1), L("orders", 2)])}

    # Equal-valued rows keep distinct combinations: 30a+30b and 60 both hit 60.
    items = [
        ({"id": "a"}, 30),
        ({"id": "b"}, 30),
        ({"id": "c"}, 60),
    ]
    combos = minimal_sum_combos(items, 60)
    assert {tuple(m["id"] for m in c) for c in combos} == {("a", "b"), ("c",)}
    # Negative values: only the full set sums to the target.
    items = [({"id": "a"}, 50), ({"id": "b"}, 30), ({"id": "c"}, -10)]
    combos = minimal_sum_combos(items, 70)
    assert {tuple(m["id"] for m in c) for c in combos} == {("a", "b", "c")}

    # Zero target: the empty selection is never a combination — the minimal
    # witness is the non-empty cancelling pair.
    items = [({"id": "a"}, 5), ({"id": "b"}, -5)]
    combos = minimal_sum_combos(items, 0)
    assert {tuple(m["id"] for m in c) for c in combos} == {("a", "b")}


def test_aggregate_sum_zero_total_witness():
    """A zero total from mixed signs is witnessed by the non-empty pair."""
    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [
                {"id": 1, "v": 5},
                {"id": 2, "v": -5},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "t"},
        "aggregates": [{"fn": "sum", "column": "v", "as": "total"}],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    assert rows[0]["data"] == (0,)
    # Not the trivial family from the empty combination: both rows pinned.
    assert rows[0]["witnesses"] == {frozenset([L("t", 1), L("t", 2)])}
    hs = minimal_hitting_sets(rows[0]["witnesses"])
    assert hs == [frozenset([L("t", 1)]), frozenset([L("t", 2)])]


def test_aggregate_null_components():
    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [
                {"id": 1, "v": 50},
                {"id": 2, "v": None},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "t"},
        "aggregates": [
            {"fn": "count", "column": "v", "as": "n"},
            {"fn": "sum", "column": "v", "as": "total"},
        ],
    }
    ev = make_evaluator(tables)
    rows = ev.eval(plan)
    # count(v)=1 and sum=50 ignore the NULL row: only row 1 is pinned.
    assert rows[0]["data"] == (1, 50)
    assert rows[0]["witnesses"] == {frozenset([L("t", 1)])}

    # All-NULL sum/min/max: value NULL, only group existence is pinned.
    plan_null = {
        "type": "aggregate",
        "input": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "v", "op": "is_null"},
        },
        "aggregates": [
            {"fn": "count", "column": "v", "as": "n"},
            {"fn": "sum", "column": "v", "as": "total"},
        ],
    }
    rows = ev.eval(plan_null)
    assert rows[0]["data"] == (0, None)
    assert rows[0]["witnesses"] == {frozenset([L("t", 2)])}


def test_mixed_sign_sum_pool_reduction():
    """5, 2, -2 + ten zero rows + one NULL row: only the three non-zero
    labels survive candidate reduction for a plain sum."""
    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [{"id": 1, "v": 5}, {"id": 2, "v": 2}, {"id": 3, "v": -2}]
            + [{"id": i, "v": 0} for i in range(4, 14)]
            + [{"id": 14, "v": None}],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "t"},
        "aggregates": [{"fn": "sum", "column": "v", "as": "total"}],
    }
    ev = make_evaluator(tables)
    pool = ev.mixed_sign_sum_pool(plan, (5,))
    assert pool is not None
    assert pool.group_member_count == 14
    assert pool.active_member_count == 3
    assert pool.reduced == {L("t", 1), L("t", 2), L("t", 3)}
    # The full pool covers all group-member witnesses (existence includes the
    # NULL row); the reduced pool drops ten zero rows plus the NULL row.
    assert len(pool.full) == 14
    assert pool.dropped_inert_count == 11

    # Same-sign sums (even with zero padding) do not enter the exact path.
    tables_pos = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [{"id": 1, "v": 5}] + [{"id": i, "v": 0} for i in range(2, 14)],
        }
    ]
    ev_pos = make_evaluator(tables_pos)
    assert ev_pos.mixed_sign_sum_pool(plan, (5,)) is None


def test_mixed_sign_sum_pool_reduction_count_star():
    """Under count(*) every member is active: zero rows stay in the pool."""
    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [{"id": 1, "v": 5}, {"id": 2, "v": 2}, {"id": 3, "v": -2}]
            + [{"id": i, "v": 0} for i in range(4, 14)],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {"type": "scan", "table": "t"},
        "aggregates": [
            {"fn": "count", "column": "*", "as": "n"},
            {"fn": "sum", "column": "v", "as": "total"},
        ],
    }
    ev = make_evaluator(tables)
    pool = ev.mixed_sign_sum_pool(plan, (13, 5))
    assert pool is not None
    assert pool.active_member_count == 13
    assert len(pool.reduced) == 13


def test_mixed_sign_sum_pool_self_join_selective_gate_keeps_zero_pair():
    """Self-join: the zero-valued row4/row4 member is inert by value, so the
    provisional active pool excludes ledger[4]; the selective inert-label
    gate restores exactly ledger[4] (the zero self-pair that keeps the zero
    target alive after deleting ledger[1] removes the +5/-5 members together).
    Independent padding zero members that cannot sustain the target stay
    reduced out (see the 18-zero-pair regression test in test_api.py)."""
    tables = [
        {
            "table": "ledger",
            "primary_key": ["id"],
            "columns": ["id", "k", "amount"],
            "rows": [
                {"id": 1, "k": 1, "amount": 0},
                {"id": 2, "k": 1, "amount": 5},
                {"id": 3, "k": 1, "amount": -5},
                {"id": 4, "k": 2, "amount": 0},
            ],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {
            "type": "filter",
            "predicate": {
                "or": [
                    {
                        "and": [
                            {"column": "id", "op": "=", "value": 1},
                            {"column": "right.id", "op": ">=", "value": 2},
                        ]
                    },
                    {"column": "id", "op": "=", "value": 4},
                ]
            },
            "input": {
                "type": "join",
                "left": {"type": "scan", "table": "ledger"},
                "right": {"type": "scan", "table": "ledger"},
                "on": [{"left": "k", "right": "k"}],
            },
        },
        "aggregates": [{"fn": "sum", "column": "right.amount", "as": "total"}],
    }
    ev = make_evaluator(tables)
    pool = ev.mixed_sign_sum_pool(plan, (0,))
    assert pool is not None
    assert pool.full == {L("ledger", 1), L("ledger", 2), L("ledger", 3), L("ledger", 4)}
    assert pool.active_labels == {L("ledger", 1), L("ledger", 2), L("ledger", 3)}
    # The selective gate widened exactly by the zero self-pair label.
    assert pool.reduced == {
        L("ledger", 1), L("ledger", 2), L("ledger", 3), L("ledger", 4)
    }
    assert pool.dropped_inert_count == 0
    assert pool.group_member_count == 3
    assert pool.active_member_count == 2


def test_mixed_sign_sum_pool_shared_left_prunes_dependent_inert():
    """Self-join where every member shares the left source row: 19 non-zero
    members (ten +9, nine -10) plus two zero members, all with row 1 in
    their witnesses. The active pool is exactly 20 labels; the selective
    gate must prune the 2**19 supersets of the cover {ledger[1]} by witness
    dependency (any mask containing ledger[1] also kills both zero members,
    so no inert survivor can reproduce total=0) instead of overflowing the
    mask budget and widening to the 22-label full pool. The zero labels
    ledger[21]/ledger[22] stay reduced out: no minimal deletion set
    contains them (deleting row 1 alone already empties the group)."""
    rows = [{"id": 1, "k": 1, "amount": 0}]
    rows += [{"id": i, "k": 1, "amount": 9} for i in range(2, 12)]
    rows += [{"id": i, "k": 1, "amount": -10} for i in range(12, 21)]
    rows += [{"id": i, "k": 1, "amount": 0} for i in (21, 22)]
    tables = [
        {
            "table": "ledger",
            "primary_key": ["id"],
            "columns": ["id", "k", "amount"],
            "rows": rows,
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {
            "type": "project",
            "columns": [
                {"from": "right.id", "as": "rid"},
                {"from": "right.amount", "as": "amt"},
            ],
            "input": {
                "type": "filter",
                "predicate": {
                    "and": [
                        {"column": "id", "op": "=", "value": 1},
                        {"column": "right.id", "op": ">=", "value": 2},
                    ]
                },
                "input": {
                    "type": "join",
                    "left": {"type": "scan", "table": "ledger"},
                    "right": {"type": "scan", "table": "ledger"},
                    "on": [{"left": "k", "right": "k"}],
                },
            },
        },
        "aggregates": [{"fn": "sum", "column": "amt", "as": "total"}],
    }
    ev = make_evaluator(tables)
    pool = ev.mixed_sign_sum_pool(plan, (0,))
    assert pool is not None
    assert pool.group_member_count == 21
    assert pool.active_member_count == 19
    assert pool.full == {L("ledger", i) for i in range(1, 23)}
    assert pool.active_labels == {L("ledger", i) for i in range(1, 21)}
    # The gate examined only the two minimal covers and one superset before
    # pruning ({1}, {2..20}, {1..20}) — far below the 200k mask budget.
    assert pool.gate_candidates == 3
    # The zero labels are proven irrelevant: the pool stays the 20 active
    # labels and fits the enumeration cap exactly.
    assert pool.reduced == {L("ledger", i) for i in range(1, 21)}
    assert pool.dropped_inert_count == 2


def test_mixed_sign_sum_pool_fast_path_proves_inert_irrelevant():
    """When no non-empty subset of the inert members can reproduce the
    target (here: zero-valued members vs target total 7), the selective
    gate returns the active pool without examining a single mask."""
    rows = [
        {"id": 1, "k": 1, "amount": 0},
        {"id": 2, "k": 1, "amount": 5},
        {"id": 3, "k": 1, "amount": -5},
        {"id": 4, "k": 2, "amount": 7},
    ]
    rows += [{"id": i, "k": i, "amount": 0} for i in range(5, 23)]
    predicate = {"or": [
        {
            "and": [
                {"column": "id", "op": "=", "value": 1},
                {"column": "right.id", "op": ">=", "value": 2},
            ]
        },
        {"column": "id", "op": "=", "value": 4},
    ]}
    predicate["or"] += [
        {"column": "id", "op": "=", "value": i} for i in range(5, 23)
    ]
    tables = [
        {
            "table": "ledger",
            "primary_key": ["id"],
            "columns": ["id", "k", "amount"],
            "rows": rows,
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {
            "type": "project",
            "columns": [{"from": "right.amount", "as": "amt"}],
            "input": {
                "type": "filter",
                "predicate": predicate,
                "input": {
                    "type": "join",
                    "left": {"type": "scan", "table": "ledger"},
                    "right": {"type": "scan", "table": "ledger"},
                    "on": [{"left": "k", "right": "k"}],
                },
            },
        },
        "aggregates": [{"fn": "sum", "column": "amt", "as": "total"}],
    }
    ev = make_evaluator(tables)
    pool = ev.mixed_sign_sum_pool(plan, (7,))
    assert pool is not None
    assert pool.reduced == {L("ledger", i) for i in range(1, 5)}
    assert pool.gate_candidates == 0
    assert pool.dropped_inert_count == 18


def test_aggregate_sum_minimal_combos_value_compression():
    """Repeated values: minimality is decided per value multiset, then
    expanded to row combinations (equal-valued rows stay distinct)."""
    from app.evaluator import minimal_sum_combos

    # 2p = 3q only at p=3, q=2: the unique non-zero minimal combination is
    # all five rows; each zero row is a singleton combination of its own.
    items = [
        ({"id": "a"}, 2),
        ({"id": "b"}, 2),
        ({"id": "c"}, 2),
        ({"id": "d"}, -3),
        ({"id": "e"}, -3),
        ({"id": "f"}, 0),
        ({"id": "g"}, 0),
    ]
    combos = minimal_sum_combos(items, 0)
    assert {tuple(m["id"] for m in c) for c in combos} == {
        ("a", "b", "c", "d", "e"),
        ("f",),
        ("g",),
    }
    # Equal positive values expand to every choice of rows: target 4 with
    # three 2-rows gives C(3,2) = 3 minimal combinations.
    items = [({"id": ch}, 2) for ch in "abc"]
    combos = minimal_sum_combos(items, 4)
    assert {tuple(m["id"] for m in c) for c in combos} == {
        ("a", "b"),
        ("a", "c"),
        ("b", "c"),
    }


def test_mixed_sign_sum_pool_projected_merged_groups():
    """When the projection drops the group key, groups A=(5,2,-2) and B=(5)
    merge into the single projected row total=5. The pool walk must locate
    BOTH origin groups by pushing aggregate rows forward through the
    projection: it covers all four labels (three active A rows + B's row),
    even though the target group key is unrecoverable from the projected
    data. Independent outsider groups are not part of the pool."""
    tables = [
        {
            "table": "orders",
            "primary_key": ["id"],
            "columns": ["id", "grp", "v"],
            "rows": [
                {"id": 1, "grp": "A", "v": 5},
                {"id": 2, "grp": "A", "v": 2},
                {"id": 3, "grp": "A", "v": -2},
                {"id": 4, "grp": "B", "v": 5},
                # independent outsider group: same-sign, disjoint labels
                {"id": 5, "grp": "C", "v": 9},
                {"id": 6, "grp": "C", "v": 1},
            ],
        }
    ]
    plan = {
        "type": "project",
        "columns": [{"from": "total", "as": "total"}],
        "input": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["grp"],
            "aggregates": [{"fn": "sum", "column": "v", "as": "total"}],
        },
    }
    ev = make_evaluator(tables)
    pool = ev.mixed_sign_sum_pool(plan, (5,))
    assert pool is not None
    assert pool.full == {L("orders", 1), L("orders", 2), L("orders", 3), L("orders", 4)}
    assert pool.reduced == {
        L("orders", 1), L("orders", 2), L("orders", 3), L("orders", 4)
    }
    # Every origin member is non-zero, so all four labels are active; the
    # outsider group C stays out of every pool component.
    assert pool.active_labels == {
        L("orders", 1), L("orders", 2), L("orders", 3), L("orders", 4)
    }
    # Members are counted across the origin groups: 3 in A + 1 in B.
    assert pool.group_member_count == 4
    assert pool.active_member_count == 4

    # Same-sign origins (B-like groups only) still take the hitting-set path.
    tables_same = [
        {
            "table": "orders",
            "primary_key": ["id"],
            "columns": ["id", "grp", "v"],
            "rows": [
                {"id": 1, "grp": "A", "v": 5},
                {"id": 2, "grp": "A", "v": 2},
                {"id": 3, "grp": "B", "v": 7},
            ],
        }
    ]
    plan_same = {
        "type": "project",
        "columns": [{"from": "total", "as": "total"}],
        "input": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["grp"],
            "aggregates": [{"fn": "sum", "column": "v", "as": "total"}],
        },
    }
    ev_same = make_evaluator(tables_same)
    assert ev_same.mixed_sign_sum_pool(plan_same, (7,)) is None


def test_empty_aggregate_input():
    from app.errors import ApiError

    tables = [
        {
            "table": "t",
            "primary_key": ["id"],
            "columns": ["id", "v"],
            "rows": [{"id": 1, "v": 1}],
        }
    ]
    plan = {
        "type": "aggregate",
        "input": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "v", "op": "=", "value": 99},
        },
        "aggregates": [{"fn": "count", "column": "*", "as": "n"}],
    }
    ev = make_evaluator(tables)
    try:
        ev.eval(plan)
        assert False, "expected ApiError"
    except ApiError as e:
        assert e.code == "EMPTY_AGGREGATE"
