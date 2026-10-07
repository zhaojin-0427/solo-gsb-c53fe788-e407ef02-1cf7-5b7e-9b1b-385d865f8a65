"""HTTP-level tests using FastAPI TestClient."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["PROVENANCE_DB"] = tmp.name
    # Import after env is set so the store picks up the temp DB.
    import importlib

    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.store.close()
    os.unlink(tmp.name)


BASE_REQUEST = {
    "idempotency_key": "key-join-1",
    "tables": [
        {
            "table": "orders",
            "primary_key": ["oid"],
            "columns": [
                {"name": "oid", "type": "int"},
                {"name": "cust", "type": "int"},
                {"name": "amt", "type": "int"},
            ],
            "rows": [
                {"oid": 1, "cust": 10, "amt": 5},
                {"oid": 2, "cust": 10, "amt": 7},
                {"oid": 3, "cust": 20, "amt": 9},
            ],
        },
        {
            "table": "customers",
            "primary_key": ["id"],
            "columns": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "string"},
            ],
            "rows": [{"id": 10, "name": "A"}, {"id": 20, "name": "B"}],
        },
    ],
    "plan": {
        "type": "join",
        "left": {"type": "scan", "table": "orders"},
        "right": {"type": "scan", "table": "customers"},
        "on": [{"left": "cust", "right": "id"}],
    },
}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.content == b'{"status":"ok"}'


def test_join_query_and_counterfactual(client):
    req = dict(BASE_REQUEST)
    req["explain"] = {"row": {"cust": 10, "oid": 1, "amt": 5, "id": 10, "name": "A"}}
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["row_count"] == 3
    cf = body["counterfactual"]
    # The joined output oid=1 has exactly one minimal witness, hence two
    # singleton minimal deletion sets.
    assert cf["minimal_deletion_count"] == 2
    deleted = {tuple(sorted(d)) for d in cf["minimal_deletion_sets"]}
    assert deleted == {("customers[10]",), ("orders[1]",)}
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
        # deleting customer 10 removes both oid=1,2 rows; deleting order 1 only one
        if sc["delete"] == ["customers[10]"]:
            assert sc["result"]["row_count"] == 1
        else:
            assert sc["result"]["row_count"] == 2


def test_idempotent_replay_byte_identical(client):
    req = {**BASE_REQUEST, "idempotency_key": "key-replay-1"}
    r1 = client.post("/query", json=req)
    r2 = client.post("/query", json=req)
    assert r1.status_code == 200
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_idempotency_conflict(client):
    modified = dict(BASE_REQUEST)
    modified["idempotency_key"] = "key-conflict-1"
    r1 = client.post("/query", json=modified)
    assert r1.status_code == 200
    modified = {
        **modified,
        "tables": [
            {
                **BASE_REQUEST["tables"][0],
                "rows": BASE_REQUEST["tables"][0]["rows"]
                + [{"oid": 4, "cust": 20, "amt": 1}],
            },
            BASE_REQUEST["tables"][1],
        ],
    }
    r2 = client.post("/query", json=modified)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_key_order_invariant(client):
    base = {**BASE_REQUEST, "idempotency_key": "key-shuffle-a"}
    r1 = client.post("/query", json=base)
    # same content, shuffled key order in a table row
    shuffled = {
        **BASE_REQUEST,
        "idempotency_key": "key-shuffle-b",
        "tables": [
            {
                **BASE_REQUEST["tables"][0],
                "rows": [
                    {"amt": 5, "cust": 10, "oid": 1},
                    {"oid": 2, "cust": 10, "amt": 7},
                    {"oid": 3, "cust": 20, "amt": 9},
                ],
            },
            BASE_REQUEST["tables"][1],
        ],
    }
    r2 = client.post("/query", json=shuffled)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content


def test_duplicate_primary_key(client):
    bad = {
        "idempotency_key": "key-dup-pk",
        "tables": [
            {
                "table": "t",
                "primary_key": ["id"],
                "columns": ["id"],
                "rows": [{"id": 1}, {"id": 1}],
            }
        ],
        "plan": {"type": "scan", "table": "t"},
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "DUPLICATE_PRIMARY_KEY"


def test_illegal_reference(client):
    bad = {
        "idempotency_key": "key-bad-ref",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id", "v"],
             "rows": [{"id": 1, "v": 1}]}
        ],
        "plan": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "nope", "op": "=", "value": 1},
        },
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ILLEGAL_REFERENCE"


def test_unknown_table(client):
    bad = {
        "idempotency_key": "key-bad-table",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id"],
             "rows": [{"id": 1}]}
        ],
        "plan": {"type": "scan", "table": "other"},
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_TABLE"


def test_sum_type_error(client):
    bad = {
        "idempotency_key": "key-sum-type",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id", "v"],
             "rows": [{"id": 1, "v": "x"}]}
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "t"},
            "aggregates": [{"fn": "sum", "column": "v", "as": "s"}],
        },
    }
    r = client.post("/query", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TYPE_ERROR"


def test_null_filter_3vl(client):
    req = {
        "idempotency_key": "key-null-3vl",
        "tables": [
            {
                "table": "t",
                "primary_key": ["id"],
                "columns": ["id", "v"],
                "rows": [{"id": 1, "v": 5}, {"id": 2, "v": None}, {"id": 3, "v": 9}],
            }
        ],
        "plan": {
            "type": "filter",
            "input": {"type": "scan", "table": "t"},
            "predicate": {"column": "v", "op": "<>", "value": 5},
        },
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200
    rows = r.json()["result"]["rows"]
    assert [x["row"]["id"] for x in rows] == [3]


def test_projected_group_key_merge_counterfactual(client):
    """Regression for the projected-merged-target false negative.

    Rows 1..3 form group A with amounts 5, 2, -2 (sum 5); row 4 forms group
    B with amount 5. The aggregate groups on grp but the project keeps only
    total, so both groups merge into the single presented row {"total": 5}.
    The merged witness family is just (s1) ∨ (s4), so a witness-only engine
    cannot reverse-engineer the dropped group keys and reports only the
    witness hit {orders[1], orders[4]}. Recomputing the whole plan after
    deleting {orders[2], orders[4]} (A -> 3) or {orders[3], orders[4]}
    (A -> 7) also makes the target disappear: all three minimal two-row
    deletion sets must be returned, stably label-sorted, with recomputed
    scenarios.
    """
    req = {
        "idempotency_key": "key-proj-merged-5",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["oid"],
                "columns": [
                    {"name": "oid", "type": "int"},
                    {"name": "grp", "type": "string"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": [
                    {"oid": 1, "grp": "A", "amount": 5},
                    {"oid": 2, "grp": "A", "amount": 2},
                    {"oid": 3, "grp": "A", "amount": -2},
                    {"oid": 4, "grp": "B", "amount": 5},
                ],
            }
        ],
        "plan": {
            "type": "project",
            "columns": [{"from": "total", "as": "total"}],
            "input": {
                "type": "aggregate",
                "input": {"type": "scan", "table": "ledger"},
                "group_by": ["grp"],
                "aggregates": [
                    {"fn": "sum", "column": "amount", "as": "total"}
                ],
            },
        },
        "explain": {"row": {"total": 5}},
    }
    r1 = client.post("/query", json=req)
    assert r1.status_code == 200, r1.text
    body = r1.json()
    # The presented result is one merged row whose witnesses are the two
    # singleton groups that minimally produce total=5.
    assert body["result"]["row_count"] == 1
    target = body["result"]["rows"][0]
    assert target["row"] == {"total": 5}
    assert target["provenance"]["labels_structured"] == [
        ["ledger[1]"],
        ["ledger[4]"],
    ]
    cf = body["counterfactual"]
    assert cf["target"] == {"total": 5}
    assert cf["target_index"] == 0
    assert cf["minimal_deletion_count"] == 3
    # Stable label-tuple order: [1,4] < [2,4] < [3,4].
    assert cf["minimal_deletion_sets"] == [
        ["ledger[1]", "ledger[4]"],
        ["ledger[2]", "ledger[4]"],
        ["ledger[3]", "ledger[4]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("ledger[1]", "ledger[4]")]["result"]["rows"] == [
        {"total": 0}
    ]
    assert by_delete[("ledger[2]", "ledger[4]")]["result"]["rows"] == [
        {"total": 3}
    ]
    assert by_delete[("ledger[3]", "ledger[4]")]["result"]["rows"] == [
        {"total": 7}
    ]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
        assert sc["result"]["row_count"] == 1
    # Idempotent replay stays byte-identical.
    r2 = client.post("/query", json=req)
    assert r2.status_code == 200
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_projected_merge_unrelated_group_not_422(client):
    """Groups merged into OTHER projected rows must not enter the candidate
    pool: twenty extra positive rows in an unrelated group (total 20, not 5)
    neither produce extra deletion sets nor inflate the pool past the cap —
    the three minimal pairs are still answered with 200."""
    req = {
        "idempotency_key": "key-proj-merged-unrelated",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["oid"],
                "columns": [
                    {"name": "oid", "type": "int"},
                    {"name": "grp", "type": "string"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": [
                    {"oid": 1, "grp": "A", "amount": 5},
                    {"oid": 2, "grp": "A", "amount": 2},
                    {"oid": 3, "grp": "A", "amount": -2},
                    {"oid": 4, "grp": "B", "amount": 5},
                ]
                + [{"oid": oid, "grp": "C", "amount": 1} for oid in range(5, 25)],
            }
        ],
        "plan": {
            "type": "project",
            "columns": [{"from": "total", "as": "total"}],
            "input": {
                "type": "aggregate",
                "input": {"type": "scan", "table": "ledger"},
                "group_by": ["grp"],
                "aggregates": [
                    {"fn": "sum", "column": "amount", "as": "total"}
                ],
            },
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_sets"] == [
        ["ledger[1]", "ledger[4]"],
        ["ledger[2]", "ledger[4]"],
        ["ledger[3]", "ledger[4]"],
    ]


def test_projected_merge_null_group_key_counterfactual(client):
    """NULL group keys form their own group; after the key is projected away
    a NULL-keyed mixed group still merges correctly and its candidates are
    traced by forward replay."""
    req = {
        "idempotency_key": "key-proj-merged-null-key",
        "tables": [
            {
                "table": "t",
                "primary_key": ["id"],
                "columns": ["id", "g", "v"],
                "rows": [
                    {"id": 1, "g": None, "v": 5},
                    {"id": 2, "g": None, "v": 2},
                    {"id": 3, "g": None, "v": -2},
                    {"id": 4, "g": "B", "v": 5},
                ],
            }
        ],
        "plan": {
            "type": "project",
            "columns": [{"from": "total", "as": "total"}],
            "input": {
                "type": "aggregate",
                "input": {"type": "scan", "table": "t"},
                "group_by": ["g"],
                "aggregates": [{"fn": "sum", "column": "v", "as": "total"}],
            },
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_sets"] == [
        ["t[1]", "t[4]"],
        ["t[2]", "t[4]"],
        ["t[3]", "t[4]"],
    ]


def test_projected_merge_both_groups_mixed_counterfactual(client):
    """When BOTH merged groups have mixed signs, killing the merged target
    requires moving each contributing group off total=5: every A-side
    singleton (5/2/-2) paired with every B-side singleton (5/3/-3) gives
    nine minimal pairs, decided by full-plan recomputation."""
    req = {
        "idempotency_key": "key-proj-merged-both-mixed",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["oid"],
                "columns": [
                    {"name": "oid", "type": "int"},
                    {"name": "grp", "type": "string"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": [
                    {"oid": 1, "grp": "A", "amount": 5},
                    {"oid": 2, "grp": "A", "amount": 2},
                    {"oid": 3, "grp": "A", "amount": -2},
                    {"oid": 4, "grp": "B", "amount": 5},
                    {"oid": 5, "grp": "B", "amount": 3},
                    {"oid": 6, "grp": "B", "amount": -3},
                ],
            }
        ],
        "plan": {
            "type": "project",
            "columns": [{"from": "total", "as": "total"}],
            "input": {
                "type": "aggregate",
                "input": {"type": "scan", "table": "ledger"},
                "group_by": ["grp"],
                "aggregates": [
                    {"fn": "sum", "column": "amount", "as": "total"}
                ],
            },
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    expected = [
        [f"ledger[{a}]", f"ledger[{b}]"]
        for a in (1, 2, 3)
        for b in (4, 5, 6)
    ]
    assert cf["minimal_deletion_sets"] == expected
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("ledger[2]", "ledger[5]")]["result"]["rows"] == [
        {"total": 2},
        {"total": 3},
    ]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_projected_merge_monotone_groups_keep_hitting_sets(client):
    """Projection-merged targets with NO mixed-sign group keep exact hitting
    sets: A = 3+2 = 5 (witness s1∧s2), B = 5 (witness s3); the merged row's
    minimal transversals are {s1,s3} and {s2,s3}."""
    req = {
        "idempotency_key": "key-proj-merged-monotone",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["oid"],
                "columns": ["oid", "grp", "amount"],
                "rows": [
                    {"oid": 1, "grp": "A", "amount": 3},
                    {"oid": 2, "grp": "A", "amount": 2},
                    {"oid": 3, "grp": "B", "amount": 5},
                ],
            }
        ],
        "plan": {
            "type": "project",
            "columns": [{"from": "total", "as": "total"}],
            "input": {
                "type": "aggregate",
                "input": {"type": "scan", "table": "ledger"},
                "group_by": ["grp"],
                "aggregates": [
                    {"fn": "sum", "column": "amount", "as": "total"}
                ],
            },
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_sets"] == [
        ["ledger[1]", "ledger[3]"],
        ["ledger[2]", "ledger[3]"],
    ]


def test_projected_merge_actual_budget_exhaustion_still_422(client):
    """Two genuinely mixed contributing groups (19 + 2 active labels) merge
    into total=5: the 21-label union really exhausts the pool cap, so the
    structured 422 is still returned — unrelated positive padding in group C
    is excluded and does not change the counts."""
    rows = [{"oid": 1, "grp": "A", "amount": 5}]
    rows += [{"oid": i, "grp": "A", "amount": 1} for i in range(2, 11)]
    rows += [{"oid": i, "grp": "A", "amount": -1} for i in range(11, 20)]
    rows += [
        {"oid": 20, "grp": "B", "amount": 7},
        {"oid": 21, "grp": "B", "amount": -2},
    ]
    rows += [{"oid": i, "grp": "C", "amount": 9} for i in range(22, 30)]
    req = {
        "idempotency_key": "key-proj-merged-422",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["oid"],
                "columns": [
                    {"name": "oid", "type": "int"},
                    {"name": "grp", "type": "string"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": rows,
            }
        ],
        "plan": {
            "type": "project",
            "columns": [{"from": "total", "as": "total"}],
            "input": {
                "type": "aggregate",
                "input": {"type": "scan", "table": "ledger"},
                "group_by": ["grp"],
                "aggregates": [
                    {"fn": "sum", "column": "amount", "as": "total"}
                ],
            },
        },
        "explain": {"row": {"total": 5}},
    }
    r1 = client.post("/query", json=req)
    assert r1.status_code == 422, r1.text
    err = r1.json()["error"]
    assert err["code"] == "COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED"
    assert err["details"]["reduced_pool_size"] == 21
    assert err["details"]["pool_limit"] == 20
    assert "counterfactual" not in r1.json()
    r2 = client.post("/query", json=req)
    assert r2.status_code == 422
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_aggregate_counterfactual_group_deletion(client):
    req = {
        "idempotency_key": "key-agg-cf",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "cid", "amount"],
                "rows": [
                    {"oid": 1, "cid": 10, "amount": 5},
                    {"oid": 2, "cid": 10, "amount": 7},
                    {"oid": 3, "cid": 20, "amount": 9},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["cid"],
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"cid": 10, "total": 12}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    # sum=12 needs both members: one conjunctive witness, two singleton
    # minimal deletion sets — deleting either row changes the total.
    assert cf["target_provenance"]["dnf"] == "(s1 ∧ s2)"
    assert cf["minimal_deletion_sets"] == [["orders[1]"], ["orders[2]"]]
    for scenario in cf["scenarios"]:
        assert scenario["target_present"] is False
        assert scenario["result"]["row_count"] == 2
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [
        {"cid": 10, "total": 7},
        {"cid": 20, "total": 9},
    ]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [
        {"cid": 10, "total": 5},
        {"cid": 20, "total": 9},
    ]


def test_aggregate_count_sum_counterfactual(client):
    """The 50/30 case: count=2 and sum=80 must be one conjunctive witness."""
    req = {
        "idempotency_key": "key-agg-50-30",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "cid", "amount"],
                "rows": [
                    {"oid": 1, "cid": 10, "amount": 50},
                    {"oid": 2, "cid": 10, "amount": 30},
                    {"oid": 3, "cid": 20, "amount": 10},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["cid"],
            "aggregates": [
                {"fn": "count", "column": "*", "as": "n"},
                {"fn": "sum", "column": "amount", "as": "total"},
            ],
        },
        "explain": {"row": {"cid": 10, "n": 2, "total": 80}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["row_count"] == 2
    target = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 10)
    assert target["witness_count"] == 1
    assert target["provenance"]["dnf"] == "(s1 ∧ s2)"
    assert target["minimal_witnesses"][0]["labels"] == ["orders[1]", "orders[2]"]
    # The other group keeps its own single-row witness.
    other = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 20)
    assert other["provenance"]["dnf"] == "(s3)"

    cf = body["counterfactual"]
    assert cf["minimal_deletion_sets"] == [["orders[1]"], ["orders[2]"]]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
    # Recomputation agrees: removing either row changes count and sum.
    assert by_delete[("orders[1]",)]["result"]["rows"] == [
        {"cid": 10, "n": 1, "total": 30},
        {"cid": 20, "n": 1, "total": 10},
    ]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [
        {"cid": 10, "n": 1, "total": 50},
        {"cid": 20, "n": 1, "total": 10},
    ]


def test_aggregate_sum_equal_value_combos(client):
    """Equal-valued rows keep distinct minimal sum combinations."""
    req = {
        "idempotency_key": "key-agg-equal-combos",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 30},
                    {"oid": 2, "amount": 30},
                    {"oid": 3, "amount": 60},
                    {"oid": 4, "amount": -60},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 60}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["result"]["rows"][0]
    # Minimal combinations summing to 60: {orders[1], orders[2]} (30+30) and
    # {orders[3]} (60). Both equal-valued 30-rows stay in their combination.
    assert row["provenance"]["dnf"] == "(s1 ∧ s2) ∨ (s3)"
    assert row["provenance"]["dnf_structured"] == [["s1", "s2"], ["s3"]]
    cf = body["counterfactual"]
    # Mixed positive/negative values make the sum non-monotone: deleting ANY
    # single row changes the total (removing 30 leaves 30, removing 60
    # leaves 0, removing -60 leaves 120), so every singleton is a minimal
    # deletion set — decided by recomputation, not by hitting sets.
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
        ["orders[4]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"total": 30}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"total": 30}]
    assert by_delete[("orders[3]",)]["result"]["rows"] == [{"total": 0}]
    assert by_delete[("orders[4]",)]["result"]["rows"] == [{"total": 120}]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_sum_mixed_signs_zero_total(client):
    """5 and -5 in one group, sum=0: the empty combination is not a witness,
    and deleting either single row (not both) removes the target."""
    req = {
        "idempotency_key": "key-agg-mixed-zero",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "cid", "amount"],
                "rows": [
                    {"oid": 1, "cid": 10, "amount": 5},
                    {"oid": 2, "cid": 10, "amount": -5},
                    {"oid": 3, "cid": 20, "amount": 7},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "group_by": ["cid"],
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"cid": 10, "total": 0}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    target = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 10)
    # The zero total is witnessed by the non-empty pair, not the empty set.
    assert target["provenance"]["dnf"] == "(s1 ∧ s2)"
    assert target["minimal_witnesses"][0]["labels"] == ["orders[1]", "orders[2]"]
    # The other group keeps its own single-row witness.
    other = next(x for x in body["result"]["rows"] if x["row"]["cid"] == 20)
    assert other["provenance"]["dnf"] == "(s3)"
    cf = body["counterfactual"]
    # Recomputation shows deleting either single row changes the total to
    # ±5, so the minimal deletion sets are the two singletons — not the
    # two-row set the (buggy) empty-witness hitting sets reported.
    assert cf["minimal_deletion_sets"] == [["orders[1]"], ["orders[2]"]]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [
        {"cid": 10, "total": -5},
        {"cid": 20, "total": 7},
    ]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [
        {"cid": 10, "total": 5},
        {"cid": 20, "total": 7},
    ]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_sum_mixed_signs_nonmonotone_deletions(client):
    """5, 2, -2 (sum=5): deleting ANY single row changes the total, so the
    minimal deletion sets include the rows that appear in no minimal sum
    combination (the 2 and the -2)."""
    req = {
        "idempotency_key": "key-agg-mixed-nonmono",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 5},
                    {"oid": 2, "amount": 2},
                    {"oid": 3, "amount": -2},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["result"]["rows"][0]
    # Only {orders[1]} sums to 5 minimally: the witness stays a singleton.
    assert row["provenance"]["dnf"] == "(s1)"
    cf = body["counterfactual"]
    # But the sum is non-monotone: removing the 2 or the -2 changes the
    # total to 3 or 7, so those singletons are minimal deletion sets too.
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"total": 0}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"total": 3}]
    assert by_delete[("orders[3]",)]["result"]["rows"] == [{"total": 7}]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_sum_mixed_signs_nonmonotone_with_zero_padding(client):
    """Regression: 5, 2, -2 plus TEN zero rows (13 source rows > old 12-row
    cap), total=5. The zero rows are reduced out of the candidate pool (a
    zero cannot belong to a minimal deletion set for a plain sum), so the
    exact recompute search still finds the three singleton sets instead of
    falling back to the single hitting set {orders[1]}."""
    req = {
        "idempotency_key": "key-agg-mixed-13",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 5},
                    {"oid": 2, "amount": 2},
                    {"oid": 3, "amount": -2},
                ]
                + [{"oid": oid, "amount": 0} for oid in range(4, 14)],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    # Only {orders[1]} minimally sums to 5; the 2/-2/zero rows never enter
    # any minimal witness, so only orders[1] is symbolized.
    row = body["result"]["rows"][0]
    assert row["provenance"]["dnf"] == "(s1)"
    assert body["symbol_table"] == [
        {"source": "orders[1]", "symbol": "s1"},
    ]
    cf = body["counterfactual"]
    # Three singleton minimal deletion sets in label order — including the
    # rows (2 and -2) that appear in no minimal sum combination.
    assert cf["minimal_deletion_sets"] == [
        ["orders[1]"],
        ["orders[2]"],
        ["orders[3]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    # Whole-plan recomputation: totals 0 / 3 / 7.
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"total": 0}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"total": 3}]
    assert by_delete[("orders[3]",)]["result"]["rows"] == [{"total": 7}]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False


def test_sum_mixed_signs_zero_rows_active_under_count(client):
    """Reduction must keep zero rows when count(*) is also selected:
    deleting a zero row changes the count, so all 13 rows are singleton
    minimal deletion sets."""
    req = {
        "idempotency_key": "key-agg-mixed-13-count",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 5},
                    {"oid": 2, "amount": 2},
                    {"oid": 3, "amount": -2},
                ]
                + [{"oid": oid, "amount": 0} for oid in range(4, 14)],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [
                {"fn": "count", "column": "*", "as": "n"},
                {"fn": "sum", "column": "amount", "as": "total"},
            ],
        },
        "explain": {"row": {"n": 13, "total": 5}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_count"] == 13
    assert cf["minimal_deletion_sets"] == [
        [f"orders[{oid}]"] for oid in range(1, 14)
    ]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
    # Deleting a zero row keeps total=5 but changes n, so the full target
    # row (n=13, total=5) is still gone — recomputed n=12, total=5.
    zero_case = next(
        sc for sc in cf["scenarios"] if sc["delete"] == ["orders[10]"]
    )
    assert zero_case["result"]["rows"] == [{"n": 12, "total": 5}]


def test_self_join_sum_counterfactual_includes_zero_self_pair(client):
    """Regression: same-table self-join filtered to three members.

    Row 1 joins row 2 (amount 5) and row 3 (amount -5); row 4 joins itself
    (amount 0). The total is 0. Deleting rows 2 or 3 alone moves the total to
    -5 / +5, but deleting row 1 alone removes BOTH the +5 and -5 pairs (net
    zero) while the row4/row4 zero pair survives — so {ledger[1]} is not a
    solution; the minimal two-label set {ledger[1], ledger[4]} empties the
    aggregate input (EMPTY_AGGREGATE). The member-value candidate reduction
    must not drop the zero pair's label simply because its value is zero; the
    label-dependency gate widens the pool and the full-plan recomputation
    search returns all three minimal deletion sets.
    """
    req = {
        "idempotency_key": "key-selfjoin-mixed-zero",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["id"],
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "k", "type": "int"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": [
                    {"id": 1, "k": 1, "amount": 0},
                    {"id": 2, "k": 1, "amount": 5},
                    {"id": 3, "k": 1, "amount": -5},
                    {"id": 4, "k": 2, "amount": 0},
                ],
            }
        ],
        "plan": {
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
        },
        "explain": {"row": {"total": 0}},
    }
    r1 = client.post("/query", json=req)
    assert r1.status_code == 200, r1.text
    body = r1.json()
    target = body["result"]["rows"][0]
    assert target["row"] == {"total": 0}
    # Witnesses: the non-empty zero combination over members
    # {(1,2),(1,3)} -> labels {1,2,3}, and the zero self-pair {4}.
    assert target["provenance"]["labels_structured"] == [
        ["ledger[1]", "ledger[2]", "ledger[3]"],
        ["ledger[4]"],
    ]
    cf = body["counterfactual"]
    assert cf["minimal_deletion_count"] == 3
    # Ordered by the stable label-tuple key: the two-label set comes first.
    assert cf["minimal_deletion_sets"] == [
        ["ledger[1]", "ledger[4]"],
        ["ledger[2]"],
        ["ledger[3]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    # The two active singletons are recomputed whole-plan.
    assert by_delete[("ledger[2]",)]["result"]["rows"] == [{"total": -5}]
    assert by_delete[("ledger[3]",)]["result"]["rows"] == [{"total": 5}]
    assert by_delete[("ledger[2]",)]["target_present"] is False
    assert by_delete[("ledger[3]",)]["target_present"] is False
    # Deleting row 1 alone leaves the zero self-pair: the target survives,
    # which is exactly why this singleton is not a deletion set.
    # The two-label set removes every join member: scalar aggregate input is
    # empty, so the scenario keeps the structured EMPTY_AGGREGATE error.
    both = by_delete[("ledger[1]", "ledger[4]")]
    assert both["target_present"] is False
    assert both["recompute_error"]["code"] == "EMPTY_AGGREGATE"
    assert "result" not in both
    # Idempotent replay stays byte-identical.
    r2 = client.post("/query", json=req)
    assert r2.status_code == 200
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_self_join_sum_counterfactual_zero_padding_not_widened(client):
    """Regression for the POST /query self-join false-positive 422.

    After projection (right amount only) the filtered self-join has four
    distinct members: row1+row2 = +5, row1+row3 = -5, row4+row4 = +7, and a
    single merged zero member carrying 18 independent zero self-pairs
    (rows 5..22). Target total=7. Deleting row 1 alone removes the +5/-5
    members but the +7 self-join member sustains the target, so the old
    all-or-nothing dependency gate widened the pool to ALL 22 member labels
    and wrongly answered 422 (pool > 20). The selective gate must recognize
    that the 18 zero pairs cannot sustain total=7 and leave them reduced out;
    the exact search returns the three singleton sets with recomputed totals
    2 / 12 / 0.
    """
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
    req = {
        "idempotency_key": "key-selfjoin-mixed-7-padded",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["id"],
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "k", "type": "int"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": rows,
            }
        ],
        "plan": {
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
        },
        "explain": {"row": {"total": 7}},
    }
    r1 = client.post("/query", json=req)
    assert r1.status_code == 200, r1.text
    body = r1.json()
    target = body["result"]["rows"][0]
    assert target["row"] == {"total": 7}
    # Only +7 minimally sums to 7: the merged zero pair and the cancelling
    # members do not enter the provenance; only ledger[4] is symbolized.
    assert target["provenance"]["labels_structured"] == [["ledger[4]"]]
    cf = body["counterfactual"]
    assert cf["minimal_deletion_count"] == 3
    assert cf["minimal_deletion_sets"] == [
        ["ledger[2]"],
        ["ledger[3]"],
        ["ledger[4]"],
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("ledger[2]",)]["result"]["rows"] == [{"total": 2}]
    assert by_delete[("ledger[3]",)]["result"]["rows"] == [{"total": 12}]
    assert by_delete[("ledger[4]",)]["result"]["rows"] == [{"total": 0}]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
    # Idempotent replay stays byte-identical.
    r2 = client.post("/query", json=req)
    assert r2.status_code == 200
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_self_join_sum_shared_left_label_no_false_422(client):
    """Regression for the POST /query self-join sum false-positive 422.

    Row 1 self-joins rows 2..22 (only left=row-1 pairs are kept); rows 2..20
    hold non-zero amounts summing to 0 (ten +9, nine -10), rows 21/22 are 0.
    After projecting the right id and amount, the 19 non-zero members share
    ledger[1] in every witness, so the active pool is exactly 20 labels —
    ledger[1..20]. The two zero members (1,21) and (1,22) also depend on
    ledger[1]: any active mask containing ledger[1] kills them too, so no
    superset of the minimal cover {ledger[1]} can ever qualify. The
    selective gate must prune those 2**19 masks by witness dependency
    instead of exhausting the 200k mask budget and falling back to the
    22-label full pool (which wrongly answered 422). The exact search then
    returns the 20 singleton deletion sets; deleting row 1 empties the
    aggregate input (EMPTY_AGGREGATE).
    """
    rows = [{"id": 1, "k": 1, "amount": 0}]
    rows += [{"id": i, "k": 1, "amount": 9} for i in range(2, 12)]
    rows += [{"id": i, "k": 1, "amount": -10} for i in range(12, 21)]
    rows += [{"id": i, "k": 1, "amount": 0} for i in (21, 22)]
    req = {
        "idempotency_key": "key-selfjoin-shared-left-0",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["id"],
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "k", "type": "int"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": rows,
            }
        ],
        "plan": {
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
        },
        "explain": {"row": {"total": 0}},
    }
    r1 = client.post("/query", json=req)
    assert r1.status_code == 200, r1.text
    body = r1.json()
    target = body["result"]["rows"][0]
    assert target["row"] == {"total": 0}
    # Witnesses: the single minimal non-zero combination covering all 19
    # non-zero members (labels ledger[1..20]), plus each zero member alone.
    assert target["provenance"]["labels_structured"] == [
        [f"ledger[{i}]" for i in range(1, 21)],
        ["ledger[1]", "ledger[21]"],
        ["ledger[1]", "ledger[22]"],
    ]
    cf = body["counterfactual"]
    assert cf["minimal_deletion_count"] == 20
    # The 20 singleton minimal deletion sets, in label order.
    assert cf["minimal_deletion_sets"] == [
        [f"ledger[{i}]"] for i in range(1, 21)
    ]
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    # Deleting row 1 removes every member: scalar aggregate input is empty.
    only_row1 = by_delete[("ledger[1]",)]
    assert only_row1["target_present"] is False
    assert only_row1["recompute_error"]["code"] == "EMPTY_AGGREGATE"
    assert "result" not in only_row1
    # Deleting a +9 row moves the total to -9; deleting a -10 row to +10.
    for i in range(2, 12):
        sc = by_delete[(f"ledger[{i}]",)]
        assert sc["target_present"] is False
        assert sc["result"]["rows"] == [{"total": -9}]
    for i in range(12, 21):
        sc = by_delete[(f"ledger[{i}]",)]
        assert sc["target_present"] is False
        assert sc["result"]["rows"] == [{"total": 10}]
    # Idempotent replay stays byte-identical.
    r2 = client.post("/query", json=req)
    assert r2.status_code == 200
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_self_join_sum_zero_target_zero_pairs_still_422(client):
    """The selective gate must not become over-aggressive: when the target is
    total=0 the 18 independent zero self-pairs genuinely sustain the target
    after deleting ledger[1] (the +5/-5 members cancel), so every zero label
    is a real candidate. 18 zero labels + 4 active labels exceed the 20-label
    pool cap and the exact search must still answer a structured 422 — never
    an approximate answer and never silently dropping the multi-label
    emptiness-kill scheme.
    """
    rows = [
        {"id": 1, "k": 1, "amount": 0},
        {"id": 2, "k": 1, "amount": 5},
        {"id": 3, "k": 1, "amount": -5},
        {"id": 4, "k": 4, "amount": 0},
    ]
    rows += [{"id": i, "k": i, "amount": 0} for i in range(5, 23)]
    predicate = {"or": [
        {
            "and": [
                {"column": "id", "op": "=", "value": 1},
                {"column": "right.id", "op": ">=", "value": 2},
            ]
        },
    ]}
    predicate["or"] += [
        {"column": "id", "op": "=", "value": i} for i in range(4, 23)
    ]
    req = {
        "idempotency_key": "key-selfjoin-mixed-0-padded-422",
        "tables": [
            {
                "table": "ledger",
                "primary_key": ["id"],
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "k", "type": "int"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": rows,
            }
        ],
        "plan": {
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
        },
        "explain": {"row": {"total": 0}},
    }
    r1 = client.post("/query", json=req)
    assert r1.status_code == 422, r1.text
    err = r1.json()["error"]
    assert err["code"] == "COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED"
    assert err["details"]["reduced_pool_size"] == 22
    assert err["details"]["pool_limit"] == 20
    assert err["details"]["dropped_inert_label_count"] == 0
    assert err["details"]["dependency_gate_recomputes"] == 0
    assert err["details"]["selective_gate_candidates"] >= 1
    assert "counterfactual" not in r1.json()
    r2 = client.post("/query", json=req)
    assert r2.status_code == 422
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_one_to_one_join_zero_padding_does_not_inflate_pool(client):
    """5, -5 and nine 0s over a one-to-one inner join: the zero pairs are
    reduced out of the candidate pool (each active-label singleton already
    kills the target), so the answer is exactly the four single-label sets —
    no spurious COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED from pool inflation."""
    rows = [
        {"oid": 1, "cid": 1, "amount": 5},
        {"oid": 2, "cid": 2, "amount": -5},
    ]
    rows += [{"oid": i, "cid": i, "amount": 0} for i in range(3, 12)]
    req = {
        "idempotency_key": "key-join-1to1-padded",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": [
                    {"name": "oid", "type": "int"},
                    {"name": "cid", "type": "int"},
                    {"name": "amount", "type": "int"},
                ],
                "rows": rows,
            },
            {
                "table": "customers",
                "primary_key": ["id"],
                "columns": [
                    {"name": "id", "type": "int"},
                    {"name": "name", "type": "string"},
                ],
                "rows": [{"id": i, "name": f"c{i}"} for i in range(1, 12)],
            },
        ],
        "plan": {
            "type": "aggregate",
            "input": {
                "type": "join",
                "left": {"type": "scan", "table": "orders"},
                "right": {"type": "scan", "table": "customers"},
                "on": [{"left": "cid", "right": "id"}],
            },
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 0}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["minimal_deletion_count"] == 4
    assert cf["minimal_deletion_sets"] == [
        ["customers[1]"],
        ["customers[2]"],
        ["orders[1]"],
        ["orders[2]"],
    ]
    for sc in cf["scenarios"]:
        assert sc["target_present"] is False
    by_delete = {tuple(sc["delete"]): sc for sc in cf["scenarios"]}
    assert by_delete[("orders[1]",)]["result"]["rows"] == [{"total": -5}]
    assert by_delete[("orders[2]",)]["result"]["rows"] == [{"total": 5}]
    assert by_delete[("customers[1]",)]["result"]["rows"] == [{"total": -5}]
    assert by_delete[("customers[2]",)]["result"]["rows"] == [{"total": 5}]


def test_counterfactual_search_budget_exceeded_structured(client):
    """A reduced candidate pool over the cap yields a structured 422 — never
    a successful response containing the hitting-set approximation — and the
    error is pinned to the idempotency key with byte-identical replays."""
    req = {
        "idempotency_key": "key-agg-budget",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [{"oid": i, "amount": 1} for i in range(1, 21)]
                + [{"oid": 21, "amount": -1}],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}],
        },
        "explain": {"row": {"total": 19}},
    }
    r1 = client.post("/query", json=req)
    assert r1.status_code == 422, r1.text
    err = r1.json()["error"]
    assert err["code"] == "COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED"
    assert err["details"]["reduced_pool_size"] == 21
    assert err["details"]["pool_limit"] == 20
    assert err["details"]["dropped_inert_label_count"] == 0
    # No approximation: no counterfactual block, no minimal_deletion_sets.
    assert "counterfactual" not in r1.json()
    r2 = client.post("/query", json=req)
    assert r2.status_code == 422
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"


def test_aggregate_min_max_null_counterfactual(client):
    req = {
        "idempotency_key": "key-agg-min-null",
        "tables": [
            {
                "table": "orders",
                "primary_key": ["oid"],
                "columns": ["oid", "amount"],
                "rows": [
                    {"oid": 1, "amount": 50},
                    {"oid": 2, "amount": 30},
                    {"oid": 3, "amount": None},
                ],
            }
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "orders"},
            "aggregates": [{"fn": "min", "column": "amount", "as": "lo"}],
        },
        "explain": {"row": {"lo": 30}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["result"]["rows"][0]
    # min=30 is produced by orders[2] alone; the NULL row pins nothing and
    # never enters the symbol table.
    assert row["provenance"]["dnf"] == "(s1)"
    assert row["minimal_witnesses"][0]["labels"] == ["orders[2]"]
    assert body["symbol_table"] == [{"source": "orders[2]", "symbol": "s1"}]
    cf = body["counterfactual"]
    assert cf["minimal_deletion_sets"] == [["orders[2]"]]
    sc = cf["scenarios"][0]
    assert sc["target_present"] is False
    assert sc["result"]["rows"] == [{"lo": 50}]


def test_counterfactual_recompute_empty_aggregate(client):
    req = {
        "idempotency_key": "key-cf-empty-agg",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id", "v"],
             "rows": [{"id": 1, "v": 5}]}
        ],
        "plan": {
            "type": "aggregate",
            "input": {"type": "scan", "table": "t"},
            "aggregates": [{"fn": "count", "column": "*", "as": "n"}],
        },
        "explain": {"row": {"n": 1}},
    }
    r = client.post("/query", json=req)
    assert r.status_code == 200
    sc = r.json()["counterfactual"]["scenarios"][0]
    assert sc["target_present"] is False
    assert sc["recompute_error"]["code"] == "EMPTY_AGGREGATE"


def test_error_response_is_canonical_and_cached(client):
    req = {
        "idempotency_key": "key-err-cached",
        "tables": [
            {"table": "t", "primary_key": ["id"], "columns": ["id"],
             "rows": [{"id": 1}, {"id": 1}]}
        ],
        "plan": {"type": "scan", "table": "t"},
    }
    r1 = client.post("/query", json=req)
    r2 = client.post("/query", json=req)
    assert r1.status_code == 400
    assert r1.content == r2.content
    assert r2.headers["x-idempotent-replay"] == "true"
