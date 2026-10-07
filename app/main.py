"""FastAPI application: relation provenance & counterfactual explanation API."""
from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict, ValidationError

from . import canonical
from .errors import ApiError
from .evaluator import Evaluator, Label, RecomputePool, Table
from .provenance import (
    SearchBudgetExceeded,
    build_symbol_table,
    label_sort_key,
    label_to_string,
    minimal_deletion_sets_by_recompute,
    minimal_hitting_sets,
    present_row,
)
from .store import IdempotencyStore

DB_PATH = os.environ.get("PROVENANCE_DB", "/data/provenance.db")


# ---------------------------------------------------------------------------
# Request models (strict: unknown fields are rejected)
# ---------------------------------------------------------------------------

class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: Literal["int", "float", "string", "bool", "any"] = "any"


class TableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    primary_key: list[str] = Field(min_length=1)
    columns: list[str | ColumnSpec] | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ExplainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Select one output row: either its (stable) index in the sorted result,
    # or an exact full-row match.
    index: int | None = None
    row: dict[str, Any] | None = None


class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)
    tables: list[TableIn] = Field(min_length=1)
    plan: dict[str, Any]
    explain: ExplainIn | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def malformed_from_pydantic(exc: ValidationError) -> ApiError:
    issues = []
    for err in exc.errors():
        issues.append(
            {
                "path": list(err["loc"]),
                "message": err["msg"],
                "type": err["type"],
            }
        )
    return ApiError(
        "MALFORMED_REQUEST",
        "Request body does not match the required schema",
        400,
        issues=issues,
    )


def binding_fingerprint(payload: QueryIn) -> str:
    """Deterministic digest of the normalized plan + table snapshot + target.

    Key order in submitted JSON objects never affects the binding; the
    fingerprint is computed over canonical JSON.
    """
    tables_norm = [t.model_dump() for t in payload.tables]
    explain = payload.explain.model_dump() if payload.explain else None
    return canonical.fingerprint(
        {
            "plan": payload.plan,
            "tables": tables_norm,
            "explain": explain,
        }
    )


def load_tables(payload: QueryIn) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for spec in payload.tables:
        raw = spec.model_dump()
        # Pydantic stringifies ColumnSpec already; keep user ordering.
        if spec.columns is not None:
            raw["columns"] = [
                c if isinstance(c, str) else c.model_dump() for c in spec.columns
            ]
        t = Table(raw)
        if t.name in tables:
            raise ApiError(
                "MALFORMED_REQUEST",
                f"Duplicate table name {t.name!r} in submission",
                table=t.name,
            )
        tables[t.name] = t
    return tables


def resolve_target(explain: ExplainIn | None, presented: list[dict], columns: list[str]) -> int:
    assert explain is not None
    if explain.index is not None and explain.row is not None:
        raise ApiError(
            "MALFORMED_REQUEST",
            "explain must use either 'index' or 'row', not both",
        )
    if explain.index is None and explain.row is None:
        raise ApiError(
            "MALFORMED_REQUEST",
            "explain must specify an output 'index' or an exact 'row'",
        )
    if explain.index is not None:
        if explain.index < 0 or explain.index >= len(presented):
            raise ApiError(
                "TARGET_NOT_FOUND",
                f"output index {explain.index} out of range "
                f"(0..{len(presented) - 1})",
                index=explain.index,
                result_size=len(presented),
            )
        return explain.index

    match_row = explain.row
    if set(match_row.keys()) != set(columns):
        raise ApiError(
            "MALFORMED_REQUEST",
            "explain.row must contain exactly the output columns",
            expected=columns,
            received=sorted(match_row.keys()),
        )
    matches = [
        i for i, p in enumerate(presented) if p["row"] == match_row
    ]
    if not matches:
        raise ApiError(
            "TARGET_NOT_FOUND",
            "explain.row does not match any output row",
        )
    return matches[0]


def recompute_after_deletion(
    plan: dict, tables: dict[str, Table], deleted: frozenset[Label]
) -> tuple[list[str], list[dict]]:
    """Filter the submitted tables by label and rerun the full plan."""
    deleted_by_table: dict[str, set[tuple]] = {}
    for lab in deleted:
        deleted_by_table.setdefault(lab[0], set()).add(lab[1])
    filtered = {
        name: t.filtered_copy(deleted_by_table.get(name, set()))
        for name, t in tables.items()
    }
    ev = Evaluator(filtered)
    rows = ev.eval(plan)
    cols = ev.schema(plan)
    return cols, rows


def kills_target(
    plan: dict, tables: dict[str, Table], target_data: tuple, deleted: frozenset[Label]
) -> bool:
    """True when recomputing without *deleted* removes the full target row.

    A recompute error (e.g. EMPTY_AGGREGATE after the last row of a group is
    deleted) counts as the target being gone.
    """
    try:
        _, rrows = recompute_after_deletion(plan, tables, deleted)
    except ApiError:
        return True
    return not any(rr["data"] == target_data for rr in rrows)


def deletion_sets_for_target(
    payload: QueryIn, tables: dict[str, Table], ev: Evaluator, target_raw: dict
) -> list[frozenset[Label]]:
    """All inclusion-minimal source-row deletions removing the target row.

    For monotone plans (and monotone aggregates: count/min/max, sums over
    same-sign values) these are exactly the minimal hitting sets of the
    target's minimal witnesses. A sum over mixed positive/negative values is
    non-monotone — deleting a row outside every witness can still change the
    total — so its deletion sets are decided by recomputing the plan over a
    reduced candidate pool (zero/NULL/non-extremal members excluded). When
    the exact search cannot finish within the enumeration/recomputation
    budget a structured COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED error is raised;
    the hitting-set answer is never silently substituted for it.
    """
    pool = ev.mixed_sign_sum_pool(payload.plan, target_raw["data"])
    if pool is not None:
        return search_deletion_sets(payload, tables, target_raw["data"], pool)
    return minimal_hitting_sets(target_raw["witnesses"])


def search_deletion_sets(
    payload: QueryIn,
    tables: dict[str, Table],
    target_data: tuple,
    pool: RecomputePool,
) -> list[frozenset[Label]]:
    """Run the exact recomputation search, converting budget exhaustion to
    a structured 422 error carrying the pool statistics.

    The candidate pool is produced by the evaluator's *selective inert-label
    gate* (``Evaluator._selective_inert_widening``): a purely structural
    analysis over the target group's witness families, costing no plan
    recomputation. It keeps every inert-only label that can occur in an
    inclusion-minimal deletion set (e.g. the zero self-join pair that keeps a
    zero target alive after cancelling active members are removed together)
    while leaving independent padding zero members reduced out — so a large
    group padded with irrelevant zero rows neither inflates the pool past the
    enumeration cap nor spends any recomputation budget. The exhaustive
    enumeration then gets the full 10 000-recomputation budget.
    """
    from .provenance import MAX_RECOMPUTE_CALLS, MAX_RECOMPUTE_POOL

    def budget_error(e: SearchBudgetExceeded, reduced_pool: set[Label]):
        details = {
            "group_member_count": pool.group_member_count,
            "active_member_count": pool.active_member_count,
            "full_pool_size": len(pool.full),
            "dropped_inert_label_count": len(pool.full) - len(reduced_pool),
            "selective_gate_candidates": pool.gate_candidates,
            "dependency_gate_recomputes": 0,
            **e.details,
        }
        return ApiError(
            "COUNTERFACTUAL_SEARCH_BUDGET_EXCEEDED",
            "The exact counterfactual search for this non-monotone (mixed "
            "positive/negative) sum could not finish within the configured "
            "budget; no approximate deletion sets are returned. Retry with a "
            "smaller target group or split the request.",
            422,
            **details,
        )

    def kills_fn(d: frozenset[Label]) -> bool:
        return kills_target(payload.plan, tables, target_data, d)

    if len(pool.reduced) > MAX_RECOMPUTE_POOL:
        e = SearchBudgetExceeded(
            "pool_too_large",
            reduced_pool_size=len(pool.reduced),
            pool_limit=MAX_RECOMPUTE_POOL,
        )
        raise budget_error(e, pool.reduced)

    try:
        return minimal_deletion_sets_by_recompute(
            pool.reduced,
            kills_fn,
            max_calls=MAX_RECOMPUTE_CALLS,
        )
    except SearchBudgetExceeded as e:
        raise budget_error(e, pool.reduced)


def execute_query(payload: QueryIn) -> dict[str, Any]:
    tables = load_tables(payload)
    ev = Evaluator(tables)
    rows = ev.eval(payload.plan)
    columns = ev.schema(payload.plan)

    # Global symbol table over every source row referenced by any witness.
    all_labels: set[Label] = set()
    for r in rows:
        for w in r["witnesses"]:
            all_labels.update(w)
    symbols = build_symbol_table(all_labels)

    presented = [present_row(r, columns, symbols) for r in rows]

    body: dict[str, Any] = {
        "result": {
            "columns": columns,
            "row_count": len(presented),
            "rows": presented,
        },
        "symbol_table": [
            {"symbol": sym, "source": label_to_string(lab)}
            for lab, sym in sorted(symbols.items(), key=lambda kv: kv[1])
        ],
    }

    if payload.explain is not None:
        target_idx = resolve_target(payload.explain, presented, columns)
        target_raw = rows[target_idx]
        target_presented = presented[target_idx]
        deletions = deletion_sets_for_target(payload, tables, ev, target_raw)

        scenarios = []
        for h in deletions:
            deleted_sorted = sorted(h, key=label_sort_key)
            try:
                rc, rrows = recompute_after_deletion(payload.plan, tables, h)
                target_present = any(rr["data"] == target_raw["data"] for rr in rrows)
                recomputed = [
                    {c: v for c, v in zip(rc, rr["data"])} for rr in rrows
                ]
                scenarios.append(
                    {
                        "delete": [label_to_string(l) for l in deleted_sorted],
                        "target_present": target_present,
                        "result": {
                            "columns": rc,
                            "row_count": len(recomputed),
                            "rows": recomputed,
                        },
                    }
                )
            except ApiError as e:
                # Deleting rows may make an aggregate input empty.
                scenarios.append(
                    {
                        "delete": [label_to_string(l) for l in deleted_sorted],
                        "target_present": False,
                        "recompute_error": {
                            "code": e.code,
                            "message": e.message,
                        },
                    }
                )

        body["counterfactual"] = {
            "target": target_presented["row"],
            "target_index": target_idx,
            "target_provenance": target_presented["provenance"],
            "minimal_deletion_count": len(deletions),
            "minimal_deletion_sets": [
                [label_to_string(l) for l in sorted(h, key=label_sort_key)]
                for h in deletions
            ],
            "scenarios": scenarios,
        }

    return body


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Relation Provenance & Counterfactual Explanation API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)
store = IdempotencyStore(DB_PATH)


@app.get("/")
def root():
    return Response(
        content=canonical.dumps(
            {
                "service": "relation-provenance-api",
                "version": "1.0.0",
                "endpoints": {
                    "POST /query": "execute plan, return provenance and optional counterfactuals",
                    "GET /health": "liveness probe",
                },
            }
        ),
        media_type="application/json",
    )


@app.get("/health")
def health():
    return Response(content=canonical.dumps({"status": "ok"}), media_type="application/json")


@app.post("/query")
async def query(request: Request):
    raw = await request.body()
    try:
        import json

        payload = QueryIn.model_validate(json.loads(raw))
    except ValidationError as e:
        err = malformed_from_pydantic(e)
        return Response(
            content=canonical.dumps(err.to_body()),
            status_code=err.status_code,
            media_type="application/json",
        )
    except Exception as e:  # JSON decode errors etc.
        err = ApiError("MALFORMED_REQUEST", f"Invalid JSON body: {e}", 400)
        return Response(
            content=canonical.dumps(err.to_body()),
            status_code=400,
            media_type="application/json",
        )

    fp = binding_fingerprint(payload)
    cached = store.get(payload.idempotency_key)
    if cached is not None:
        stored_fp, status, body = cached
        if stored_fp != fp:
            err = ApiError(
                "IDEMPOTENCY_KEY_CONFLICT",
                "Idempotency key was already bound to a different normalized "
                "request (plan, table snapshot or explain target differs)",
                409,
            )
            return Response(
                content=canonical.dumps(err.to_body()),
                status_code=409,
                media_type="application/json",
            )
        return Response(
            content=bytes(body),
            status_code=status,
            media_type="application/json",
            headers={"X-Idempotent-Replay": "true"},
        )

    try:
        result = execute_query(payload)
        body = canonical.dumps({"status": "ok", **result})
        store.put(payload.idempotency_key, fp, 200, body)
        return Response(
            content=body,
            status_code=200,
            media_type="application/json",
            headers={"X-Idempotent-Replay": "false"},
        )
    except ApiError as e:
        body = canonical.dumps(e.to_body())
        # Errors are also pinned to the idempotency key, so a retried invalid
        # request remains byte-identical.
        store.put(payload.idempotency_key, fp, e.status_code, body)
        return Response(
            content=body, status_code=e.status_code, media_type="application/json"
        )
