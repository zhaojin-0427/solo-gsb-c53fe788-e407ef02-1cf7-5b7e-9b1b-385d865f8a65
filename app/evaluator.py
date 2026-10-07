"""Relational-algebra evaluator with minimal-witness why-provenance.

Supported plan nodes (each is an object with a ``type``):

  {"type": "scan", "table": "orders"}
  {"type": "filter", "input": <node>, "predicate": <predicate tree>}
  {"type": "project", "input": <node>,
   "columns": ["a", {"from": "b", "as": "c"}]}
  {"type": "join", "left": <node>, "right": <node>,
   "on": [{"left": "id", "right": "cid"}]}
  {"type": "aggregate", "input": <node>,
   "group_by": ["cust"],
   "aggregates": [{"fn": "sum", "column": "amount", "as": "total"}]}

Every intermediate relation is a list of *labeled rows*::

    {"data": (v1, v2, ...), "witnesses": set[frozenset[label]]}

Rows with identical data are merged (set semantics); their witness sets are
unioned and pruned to inclusion-minimal witnesses. A label is a
``(table, pk-tuple)`` pair identifying one submitted source row.
"""
from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any, Iterable

from .errors import ApiError
from .values import (
    eval_predicate,
    is_number,
    strict_equal,
    tuple_sort_key,
    type_name,
)

Label = tuple[str, tuple]
Witness = frozenset[Label]

# Column descriptor: (name, declared type or None, nullable)
Col = tuple[str, str | None]


class RecomputePool:
    """Reduced candidate pool for recomputation-based deletion-set search.

    Attributes:
      full:    every source label in the target group's member witnesses;
      active_labels: labels appearing in at least one witness of an *active*
               member (a member whose deletion can change one of the target
               aggregate values). Labels in ``full - active_labels`` support
               only inert members (e.g. zero-valued join pairs) and are the
               candidates for reduction;
      reduced: labels the exact search actually enumerates. It starts equal
               to ``active_labels``; the selective inert-label gate
               (``Evaluator._selective_inert_widening``) adds back exactly
               those inert-only labels that can occur in an inclusion-minimal
               deletion set. Unlike the previous all-or-nothing dependency
               gate it does NOT widen back to ``full``: padding zero
               members that cannot sustain the target row on their own stay
               reduced out, while the labels of the zero members that keep a
               zero-valued target alive (the zero self-join pair case) are
               restored;
      group_member_count: number of input rows in the target group;
      active_member_count: number of those that can change at least one
               aggregate value of the target output;
      gate_candidates: number of active-label subsets examined by the
               selective gate (diagnostic for the structured budget error).
    """

    __slots__ = (
        "full",
        "reduced",
        "active_labels",
        "group_member_count",
        "active_member_count",
        "gate_candidates",
    )

    def __init__(
        self,
        full: set[Label],
        reduced: set[Label],
        group_member_count: int,
        active_member_count: int,
        active_labels: set[Label] | None = None,
        gate_candidates: int = 0,
    ):
        self.full = full
        self.reduced = reduced
        self.active_labels = active_labels if active_labels is not None else set(reduced)
        self.group_member_count = group_member_count
        self.active_member_count = active_member_count
        self.gate_candidates = gate_candidates

    @property
    def dropped_inert_count(self) -> int:
        return len(self.full) - len(self.reduced)


# ---------------------------------------------------------------------------
# Table loading / validation
# ---------------------------------------------------------------------------

def _coerce_value(v: Any, declared: str | None, *, where: str) -> Any:
    if v is None:
        return None
    if declared == "bool":
        if not isinstance(v, bool):
            raise ApiError("TYPE_ERROR", f"{where}: expected bool, got {type_name(v)}")
    elif declared == "int":
        if not isinstance(v, int) or isinstance(v, bool):
            raise ApiError("TYPE_ERROR", f"{where}: expected int, got {type_name(v)}")
    elif declared == "float":
        if not is_number(v):
            raise ApiError("TYPE_ERROR", f"{where}: expected number, got {type_name(v)}")
        return float(v)
    elif declared == "string":
        if not isinstance(v, str):
            raise ApiError("TYPE_ERROR", f"{where}: expected string, got {type_name(v)}")
    return v


class Table:
    def __init__(self, spec: dict):
        self.name: str = spec["table"]
        self.pk_cols: list[str] = list(spec["primary_key"])
        self.columns: list[Col] = []

        declared: dict[str, str] = {}
        for c in spec.get("columns", []):
            if isinstance(c, dict):
                declared[c["name"]] = c["type"]
                self.columns.append((c["name"], c["type"]))
            else:
                declared[c] = "any"
                self.columns.append((c, None))

        rows_in: list[dict] = spec.get("rows", [])
        # Infer columns from rows when (or beyond) declared columns.
        seen = {name for name, _ in self.columns}
        for r in rows_in:
            for k in r.keys():
                if k not in seen:
                    self.columns.append((k, None))
                    seen.add(k)

        self.colnames = [name for name, _ in self.columns]
        unknown_pk = [c for c in self.pk_cols if c not in self.colnames]
        if unknown_pk:
            raise ApiError(
                "ILLEGAL_REFERENCE",
                f"Table {self.name!r}: primary key column(s) {unknown_pk} not "
                f"among declared columns",
                table=self.name,
                columns=unknown_pk,
            )

        # rows: list of (pk-tuple, full data tuple aligned to columns, label)
        self.rows: list[tuple[tuple, tuple, Label]] = []
        seen_pks: dict[tuple, Label] = {}
        for idx, r in enumerate(rows_in):
            extra = [k for k in r if k not in self.colnames]
            if extra:
                raise ApiError(
                    "MALFORMED_REQUEST",
                    f"Table {self.name!r} row {idx}: unknown column(s) {extra}",
                    table=self.name,
                    row_index=idx,
                    columns=extra,
                )
            vals = []
            for name, typ in self.columns:
                v = r.get(name)
                vals.append(_coerce_value(v, typ, where=f"table {self.name!r} row {idx}, column {name!r}"))
            pk = tuple(r.get(c) for c in self.pk_cols)
            if any(p is None for p in pk):
                raise ApiError(
                    "NULL_PRIMARY_KEY",
                    f"Table {self.name!r} row {idx}: primary key part is null",
                    table=self.name,
                    row_index=idx,
                )
            label = (self.name, pk)
            if pk in seen_pks:
                raise ApiError(
                    "DUPLICATE_PRIMARY_KEY",
                    f"Table {self.name!r}: duplicate primary key {list(pk)}",
                    table=self.name,
                    primary_key=list(pk),
                )
            seen_pks[pk] = label
            self.rows.append((pk, tuple(vals), label))

    def filtered_copy(self, drop_pks: set[tuple]) -> "Table":
        """Same schema, rows whose primary key is not in *drop_pks*."""
        clone = Table.__new__(Table)
        clone.name = self.name
        clone.pk_cols = self.pk_cols
        clone.columns = self.columns
        clone.colnames = self.colnames
        clone.rows = [r for r in self.rows if r[0] not in drop_pks]
        return clone


# ---------------------------------------------------------------------------
# Witness utilities
# ---------------------------------------------------------------------------

def prune(witnesses: Iterable[Witness]) -> set[Witness]:
    """Keep only inclusion-minimal witnesses."""
    uniq = set(witnesses)
    return {w for w in uniq if not any(o < w for o in uniq)}


def label_can_hit_minimally(label: Label, edges: Iterable[Witness]) -> bool:
    """True iff *label* occurs in some inclusion-minimal hitting set of the
    witness hypergraph *edges*.

    A label *z* is usable in a minimal transversal iff it has an incident
    edge *e* such that no *z*-free edge is contained in *e*: that edge can
    then be hit via *z* while the remaining *z*-free edges not contained in
    *e* force additional labels, so *z* never becomes redundant. An incident
    edge strictly containing a *z*-free edge is always better hit through the
    smaller edge instead.
    """
    edges = list(edges)
    for e in edges:
        if label not in e:
            continue
        if not any(o < e for o in edges if label not in o):
            return True
    return False



def cover_witnesses(rows: Iterable[dict]) -> set[Witness]:
    """Witness family of a *conjunction* of member rows.

    Every row must survive, so each witness picks one witness per row and
    unions them (cartesian product of the rows' witness families).
    """
    result: set[Witness] = {frozenset()}
    for m in rows:
        result = {a | b for a in result for b in m["witnesses"]}
    return prune(result)


def minimal_sum_combos(
    items: list[tuple[dict, Any]], target: Any
) -> list[tuple[dict, ...]]:
    """All inclusion-minimal NON-EMPTY subsets of *items* summing to *target*.

    Rows are matched by identity, so equal-valued rows yield distinct
    combinations. The empty selection is never a combination: a sum over zero
    source rows is not produced by any source row. This matters when the
    target is 0 — with mixed positive/negative values the empty set would
    otherwise "witness" the total and collapse the sum component to the
    trivial family, making the provenance (and the deletion sets derived
    from it) wrong.

    The enumeration works on take-counts per DISTINCT value instead of per
    row. A row-level combination is minimal iff its value multiset is
    minimal: a proper sub-multiset summing to the target would be realised
    by a proper row subset, and every proper row subset realises a proper
    sub-multiset. So the backtracking is exponential in the number of
    distinct values (not in the row count — a group of 21 members holding
    the three values 9, -10, 0 stays cheap), and each minimal take-vector
    then expands into its row-level combinations, which are all minimal.
    Once a partial selection already sums to the target no superset of it
    can be minimal, and with all values non-negative a partial sum above
    the target can never come back down; both observations prune the
    backtracking.
    """
    # Group item indices by value, preserving first-appearance order.
    group_pos: dict[Any, int] = {}
    values: list[Any] = []
    members: list[list[int]] = []
    for idx, (_, v) in enumerate(items):
        pos = group_pos.get(v)
        if pos is None:
            group_pos[v] = len(values)
            values.append(v)
            members.append([idx])
        else:
            members[pos].append(idx)
    d = len(values)
    non_negative = all(is_number(v) and v >= 0 for v in values)
    take_vectors: list[tuple[int, ...]] = []
    # Explicit-stack backtracking over per-value take-counts; each state is
    # (next group index, accumulated sum, take-counts chosen so far).
    stack: list[tuple[int, Any, tuple[int, ...]]] = [(0, 0, ())]
    while stack:
        i, acc, takes = stack.pop()
        if acc == target and any(takes):
            # Pad the unprocessed groups with zero takes so every recorded
            # vector has full length d for the minimality filter below.
            take_vectors.append(takes + (0,) * (d - i))
            continue
        if i == d or (non_negative and acc > target):
            continue
        v = values[i]
        limit = len(members[i])
        if v == 0:
            # Two or more copies of a zero value are never minimal: the
            # single-copy sub-multiset already has the same sum.
            limit = min(limit, 1)
        acc_c = acc
        for c in range(limit + 1):
            if c:
                # Repeated addition (not c * v) keeps float accumulation
                # order close to the per-row enumeration.
                acc_c += v
            stack.append((i + 1, acc_c, takes + (c,)))
    # Keep only componentwise-minimal take-vectors: a vector strictly
    # dominating another target-summing vector realises row combinations
    # that properly contain another combination, so they are not minimal.
    minimal_vectors = [
        t
        for t in take_vectors
        if not any(
            u != t and all(uc <= tc for uc, tc in zip(u, t))
            for u in take_vectors
        )
    ]
    combos: list[tuple[dict, ...]] = []
    for takes in minimal_vectors:
        choices = [
            itertools.combinations(members[g], c)
            for g, c in enumerate(takes)
            if c
        ]
        for picked in itertools.product(*choices):
            row_indices = sorted(j for part in picked for j in part)
            combos.append(tuple(items[j][0] for j in row_indices))
    return combos


def merge_relation(rows: Iterable[dict]) -> list[dict]:
    """Merge labeled rows with identical data tuples; prune witnesses."""
    by_data: dict[tuple, set[Witness]] = defaultdict(set)
    for r in rows:
        by_data[r["data"]].update(r["witnesses"])
    out = [
        {"data": data, "witnesses": prune(ws)}
        for data, ws in by_data.items()
    ]
    out.sort(key=lambda r: tuple_sort_key(r["data"]))
    return out


class Evaluator:
    def __init__(self, tables: dict[str, Table]):
        self.tables = tables
        self._cache: dict[int, list[dict]] = {}

    # -- node helpers -------------------------------------------------------

    def eval(self, node: Any) -> list[dict]:
        if not isinstance(node, dict):
            raise ApiError("MALFORMED_PLAN", "Plan node must be an object")
        nid = id(node)
        if nid in self._cache:
            return self._cache[nid]
        t = node.get("type")
        if t == "scan":
            res = self._scan(node)
        elif t == "filter":
            res = self._filter(node)
        elif t == "project":
            res = self._project(node)
        elif t == "join":
            res = self._join(node)
        elif t == "aggregate":
            res = self._aggregate(node)
        else:
            raise ApiError(
                "MALFORMED_PLAN",
                f"Unknown plan node type {t!r}; expected one of "
                "scan/filter/project/join/aggregate",
            )
        self._cache[nid] = res
        return res

    def schema(self, node: Any) -> list[str]:
        t = node.get("type")
        if t == "scan":
            table = self._require_table(node)
            return table.colnames
        if t == "filter":
            self._filter_schema(node)
            return self.schema(self._input(node))
        if t == "project":
            return [asname for _, asname in self._proj_cols(node)]
        if t == "join":
            return self._join_schema(node)[0]
        if t == "aggregate":
            return self._agg_schema(node)
        raise ApiError("MALFORMED_PLAN", f"Unknown plan node type {t!r}")

    def _input(self, node: dict) -> Any:
        if "input" not in node:
            raise ApiError("MALFORMED_PLAN", f"{node.get('type')!r} node needs 'input'")
        return node["input"]

    def _filter_schema(self, node: dict) -> None:
        """Validate predicate shape and column references against input schema."""
        pred = node.get("predicate")
        if not isinstance(pred, dict):
            raise ApiError("MALFORMED_PLAN", "filter node needs a 'predicate' object")
        names = self.schema(self._input(node))

        def check(p: Any) -> None:
            if not isinstance(p, dict) or not p:
                raise ApiError(
                    "MALFORMED_PLAN",
                    "Predicate must be a non-empty object",
                )
            compound = [k for k in p if k in ("and", "or", "not")]
            if compound:
                if len(p) != 1:
                    raise ApiError(
                        "MALFORMED_PLAN",
                        f"Compound predicate {compound[0]!r} must have no sibling keys",
                    )
                key, body = compound[0], p[compound[0]]
            elif "column" in p:
                key, body = "column", p
            else:
                raise ApiError(
                    "MALFORMED_PLAN",
                    "Predicate must have key 'and'/'or'/'not'/'column'",
                )
            if key == "and" or key == "or":
                if not isinstance(body, list) or not body:
                    raise ApiError(
                        "MALFORMED_PLAN", f"{key!r} predicate needs a non-empty list"
                    )
                for sub in body:
                    check(sub)
            elif key == "not":
                check(body)
            elif key == "column":
                if not isinstance(body, dict):
                    raise ApiError("MALFORMED_PLAN", "column predicate must be object")
                col = body.get("column")
                op = body.get("op")
                if not isinstance(col, str) or not isinstance(op, str):
                    raise ApiError(
                        "MALFORMED_PLAN", "Predicate needs string 'column' and 'op'"
                    )
                if col not in names:
                    raise ApiError(
                        "ILLEGAL_REFERENCE",
                        f"Predicate references unknown column {col!r}",
                        column=col,
                        available=names,
                    )
                if op in {"is_null", "is_not_null"}:
                    return
                if op not in {"=", "<>", "<", "<=", ">", ">="}:
                    raise ApiError("MALFORMED_PLAN", f"Unknown predicate operator {op!r}")
                if "value" not in body:
                    raise ApiError("MALFORMED_PLAN", f"Predicate {op!r} needs 'value'")
                lit = body["value"]
                if not isinstance(lit, (str, int, float, bool)) and lit is not None:
                    raise ApiError(
                        "MALFORMED_PLAN",
                        "Predicate literal must be a JSON scalar or null",
                    )
            else:
                raise ApiError("MALFORMED_PLAN", f"Unknown predicate node {key!r}")

        check(pred)

    def _require_table(self, node: dict) -> Table:
        name = node.get("table")
        if not isinstance(name, str):
            raise ApiError("MALFORMED_PLAN", "scan node needs string 'table'")
        table = self.tables.get(name)
        if table is None:
            raise ApiError(
                "UNKNOWN_TABLE",
                f"Plan references table {name!r} which was not submitted",
                table=name,
            )
        return table

    def _col_index(self, node: Any, col: str) -> tuple[int, list[str]]:
        names = self.schema(node)
        if col not in names:
            raise ApiError(
                "ILLEGAL_REFERENCE",
                f"Column {col!r} not found in node output columns {names}",
                column=col,
                available=names,
            )
        return names.index(col), names

    # -- operators ----------------------------------------------------------

    def _scan(self, node: dict) -> list[dict]:
        table = self._require_table(node)
        return [
            {"data": data, "witnesses": {frozenset({label})}}
            for _, data, label in table.rows
        ]

    def _filter(self, node: dict) -> list[dict]:
        src = self.eval(self._input(node))
        pred = node.get("predicate")
        if not isinstance(pred, dict):
            raise ApiError("MALFORMED_PLAN", "filter node needs a 'predicate' object")
        names = self.schema(self._input(node))
        out = []
        for r in src:
            row = dict(zip(names, r["data"]))
            keep = eval_predicate(pred, row)
            if keep is True:
                out.append(r)
        # filter cannot introduce witness redundancy, but keep ordering stable
        out.sort(key=lambda r: tuple_sort_key(r["data"]))
        return out

    def _proj_cols(self, node: dict) -> list:
        cols = node.get("columns")
        if not isinstance(cols, list) or not cols:
            raise ApiError(
                "MALFORMED_PLAN", "project node needs a non-empty 'columns' list"
            )
        norm = []
        for c in cols:
            if isinstance(c, str):
                norm.append((c, c))
            elif isinstance(c, dict) and isinstance(c.get("from"), str) and isinstance(c.get("as"), str):
                norm.append((c["from"], c["as"]))
            else:
                raise ApiError(
                    "MALFORMED_PLAN",
                    "project columns must be strings or {from, as} objects",
                )
        return norm

    def _proj_as(self, c: Any) -> str:
        return c if isinstance(c, str) else c["as"]

    def _project(self, node: dict) -> list[dict]:
        src_node = self._input(node)
        src = self.eval(src_node)
        names = self.schema(src_node)
        spec = self._proj_cols(node)
        idx: list[int] = []
        out_names: list[str] = []
        for frm, asname in spec:
            if frm not in names:
                raise ApiError(
                    "ILLEGAL_REFERENCE",
                    f"project references unknown column {frm!r}",
                    column=frm,
                    available=names,
                )
            if asname in out_names:
                raise ApiError(
                    "MALFORMED_PLAN", f"project output has duplicate column {asname!r}"
                )
            idx.append(names.index(frm))
            out_names.append(asname)
        produced = []
        for r in src:
            produced.append(
                {
                    "data": tuple(r["data"][i] for i in idx),
                    "witnesses": set(r["witnesses"]),
                }
            )
        return merge_relation(produced)

    def _join_aliases(self, node: dict) -> tuple[str | None, str | None]:
        left_alias = node.get("left_alias")
        right_alias = node.get("right_alias")
        for a in (left_alias, right_alias):
            if a is not None and not isinstance(a, str):
                raise ApiError("MALFORMED_PLAN", "join alias must be a string")
        return left_alias, right_alias

    def _join_schema(self, node: dict) -> tuple[list[str], list[str], list[str], list[tuple[str, str]]]:
        left_node = node.get("left")
        right_node = node.get("right")
        if left_node is None or right_node is None:
            raise ApiError("MALFORMED_PLAN", "join node needs 'left' and 'right'")
        ln = self.schema(left_node)
        rn = self.schema(right_node)
        la, ra = self._join_aliases(node)
        on = self._join_on(node, ln, rn)

        out: list[str] = list(ln)
        # Equal columns on the join keys appear once (USING semantics).
        right_key_cols = {rc for _, rc in on}
        for c in rn:
            if c in right_key_cols and c in ln:
                continue
            name = c
            if name in ln or (la and name.startswith(f"{la}.")) or (ra and name.startswith(f"{ra}.")):
                q = ra or "right"
                name = f"{q}.{c}"
                if name in ln or name in out:
                    raise ApiError(
                        "MALFORMED_PLAN",
                        f"join output column name collision on {name!r}; use "
                        "right_alias or project to disambiguate",
                    )
            out.append(name)
        return out, ln, rn, on

    def _join_on(self, node: dict, ln: list[str], rn: list[str]) -> list[tuple[str, str]]:
        on = node.get("on")
        if not isinstance(on, list) or not on:
            raise ApiError("MALFORMED_PLAN", "join node needs a non-empty 'on' list")
        pairs: list[tuple[str, str]] = []
        for p in on:
            if not isinstance(p, dict) or "left" not in p or "right" not in p:
                raise ApiError(
                    "MALFORMED_PLAN", "join 'on' entries must be {left, right} objects"
                )
            lc, rc = p["left"], p["right"]
            if lc not in ln:
                raise ApiError(
                    "ILLEGAL_REFERENCE",
                    f"join references unknown left column {lc!r}",
                    column=lc,
                    available=ln,
                )
            if rc not in rn:
                raise ApiError(
                    "ILLEGAL_REFERENCE",
                    f"join references unknown right column {rc!r}",
                    column=rc,
                    available=rn,
                )
            pairs.append((lc, rc))
        return pairs

    def _join(self, node: dict) -> list[dict]:
        out_names, ln, rn, on = self._join_schema(node)
        left = self.eval(node["left"])
        right = self.eval(node["right"])
        li = [ln.index(lc) for lc, _ in on]
        ri = [rn.index(rc) for _, rc in on]

        # Hash right side on the composite equi-key. Keys containing NULL never
        # match (SQL NULL semantics); rows of different hash values simply fail
        # to join. A TYPE_ERROR is only raised for colliding keys whose values
        # are equal by Python hash/eq but not strict JSON equality (e.g. bool
        # vs int), since that is an ambiguous, almost certainly unintended
        # cross-type comparison.
        index: dict[tuple, list[dict]] = defaultdict(list)
        for r in right:
            key = tuple(r["data"][i] for i in ri)
            if any(k is None for k in key):
                continue
            index[key].append(r)

        produced = []
        for lr in left:
            lkey = tuple(lr["data"][i] for i in li)
            if any(k is None for k in lkey):
                continue
            for rr in index.get(lkey, ()):
                # strict type check across the key pairs
                for a, b in zip(lkey, (rr["data"][i] for i in ri)):
                    if strict_equal(a, b) is False:
                        raise ApiError(
                            "TYPE_ERROR",
                            "Equi-join key values have mismatched types "
                            f"({type_name(a)} vs {type_name(b)})",
                        )
                data = list(lr["data"])
                right_key_cols = {rc for _, rc in on}
                for j, c in enumerate(rn):
                    if c in right_key_cols and c in ln:
                        continue
                    data.append(rr["data"][j])
                witnesses: set[Witness] = set()
                for lw in lr["witnesses"]:
                    for rw in rr["witnesses"]:
                        witnesses.add(frozenset(lw | rw))
                produced.append({"data": tuple(data), "witnesses": witnesses})
        return merge_relation(produced)

    # -- aggregate ----------------------------------------------------------

    _AGG_FNS = {"count", "sum", "min", "max"}

    def mixed_sign_sum_pool(self, node: Any, target_data: tuple) -> "RecomputePool | None":
        """Reduced candidate pool for the target row when its group has a
        ``sum`` over mixed positive/negative values (a non-monotone sum).

        *node* is the plan root and *target_data* the target row aligned to
        its output schema. The walk descends through ``filter``/``project``
        nodes (which neither change row identity nor dependencies) to the
        innermost ``aggregate`` producing the target. Returns ``None`` when:

        * no ``sum`` aggregate is involved (count/min/max are exact through
          hitting sets), or
        * the target group's sum values are all non-negative or all
          non-positive (the sum then changes iff a minimal-combination row is
          deleted, so hitting sets stay exact), or
        * the aggregate cannot be reached by the walk (e.g. a join above
          it), in which case the caller keeps the hitting-set answer.

        Projection-merged groups (dropped group keys)
        ----------------------------------------------
        A projection above the aggregate may drop the group key columns.
        Distinct groups producing identical projected tuples are then merged
        into one output row by set semantics, and their witness families are
        unioned — so the evaluator can no longer reverse the *single* group
        the target came from. (The previous reverse walk recovered the key
        from the projected target data and, finding it unmapped, gave up: the
        caller fell back to witness hitting sets, which under-approximate a
        non-monotone sum and silently missed deletion sets — e.g. groups
        A=(5,2,-2) and B=(5) projected to one row ``total=5`` only reported
        ``{A.5-row, B.5-row}`` while ``{A.2-row, B.5-row}`` and
        ``{A.-2-row, B.5-row}`` kill the merged row just as well.)

        Instead the aggregate rows are pushed **forward** through the same
        filter/project chain: every aggregate output whose projected tuple
        equals the target is an *origin group* of the merged row, and each
        contributes its own target aggregate values, members, active/inert
        classification and selective gate. A deletion set must remove the
        projected target produced by **every** origin group (or change each
        group's values away from it); the full-plan recompute search over the
        unioned pool adjudicates exactly on the complete projected result.

        Candidate reduction
        -------------------
        A source row that cannot change *any* aggregate value of the target
        output can never belong to an inclusion-minimal deletion set for that
        full output row, so its exclusive labels are excluded from the
        returned pool. Per aggregate over each origin group, an input member
        is *inert* when:

        * ``count(*)``: never inert (each member changes the count);
        * ``count(col)``: its ``col`` value is NULL (NULLs are not counted);
        * ``sum(col)``: its ``col`` value is NULL or zero (zero leaves the
          total unchanged; this removes arbitrary many padding zero rows);
        * ``min``/``max(col)``: its non-NULL value differs from the target
          extremum — removing it cannot move the extremum.

        Labels supporting only inert members are dropped for now; see
        ``_selective_inert_widening`` for the exact structural gate that adds
        such labels back when joins let one source label feed several
        members. The gate is run per origin group and its restored labels are
        unioned.

        Groups *outside* the target row stay out of the pool unless a join
        gives them a witness label in common with an origin group (the
        *coupled closure*): only such shared labels can delete members of
        origin and outsider simultaneously, so only their outsider groups can
        matter to a minimal deletion set. Independent outsider groups — the
        common "many unrelated groups in the same aggregate" case, including
        unrelated mixed-sign groups — are never enumerated and therefore can
        neither inflate the pool nor trigger a spurious 422.
        """
        # Walk filter/project nodes down to the aggregate producing the
        # target, recording the chain from the plan root towards it.
        chain: list[dict] = []
        cur = node
        while True:
            t = cur.get("type") if isinstance(cur, dict) else None
            if t in ("filter", "project"):
                chain.append(cur)
                cur = self._input(cur)
            elif t == "aggregate":
                agg_node = cur
                break
            else:
                return None

        aggs = self._agg_spec(agg_node)
        sum_cols = [col for fn, col, _ in aggs if fn == "sum"]
        if not sum_cols:
            return None
        gb = agg_node.get("group_by", [])
        out_schema = self._agg_schema(agg_node)
        in_names = self.schema(self._input(agg_node))
        key_idx = [in_names.index(g) for g in gb]
        agg_rows = self.eval(agg_node)

        # Push each aggregate row FORWARD through the filter/project chain
        # (chain runs root -> aggregate, so iterate it in reverse) and keep
        # exactly those whose final tuple equals the projected target: these
        # are the origin groups merged into the target row. A filter above the
        # aggregate is evaluated on the row currently flowing through it,
        # whose columns are the schema at that point of the chain.
        def project_forward(agg_data: tuple) -> tuple | None:
            data = tuple(agg_data)
            names_here = out_schema
            for step in reversed(chain):
                if step.get("type") == "filter":
                    row = dict(zip(names_here, data))
                    if eval_predicate(step["predicate"], row) is not True:
                        return None
                else:
                    spec = self._proj_cols(step)
                    vals = dict(zip(names_here, data))
                    data = tuple(vals[frm] for frm, _ in spec)
                    names_here = [asname for _, asname in spec]
            return data

        n_key = len(gb)
        origin_rows: list[dict] = []
        for r in agg_rows:
            if project_forward(r["data"]) == target_data:
                origin_rows.append(r)
        if not origin_rows:
            return None

        src_rows = self.eval(self._input(agg_node))

        def members_of(agg_row: dict) -> list[dict]:
            key = agg_row["data"][:n_key]
            return [
                r
                for r in src_rows
                if tuple(r["data"][i] for i in key_idx) == key
            ]

        # Aggregate rows carry distinct group keys, so the key tuple is a
        # stable identity (NULL parts included).
        groups: dict[tuple, list[dict]] = {
            r["data"][:n_key]: members_of(r) for r in agg_rows
        }
        origin_keys = {r["data"][:n_key] for r in origin_rows}

        # Coupled closure over shared witness labels: an outsider group can
        # only interact with the target row through labels it shares with a
        # group already in the closure. Independent outsider groups are
        # provably irrelevant and never enter the candidate pool.
        label_sets: dict[tuple, set[Label]] = {}
        for key, members in groups.items():
            labs: set[Label] = set()
            for m in members:
                for w in m["witnesses"]:
                    labs.update(w)
            label_sets[key] = labs

        closure: set[tuple] = set(origin_keys)
        changed = True
        while changed:
            changed = False
            in_labels: set[Label] = set()
            for key in closure:
                in_labels |= label_sets[key]
            for key in groups:
                if key not in closure and label_sets[key] & in_labels:
                    closure.add(key)
                    changed = True

        # The exact recomputation path is required once ANY group in the
        # coupled closure has a mixed-sign sum: deleting a shared label may
        # move origin and outsider together, so hitting sets of the merged
        # row's witnesses are not a complete characterization either way.
        mixed = False
        for col in sum_cols:
            ci = in_names.index(col)
            for key in closure:
                vals = [
                    m["data"][ci]
                    for m in groups[key]
                    if m["data"][ci] is not None
                ]
                if any(v > 0 for v in vals) and any(v < 0 for v in vals):
                    mixed = True
                    break
            if mixed:
                break
        if not mixed:
            return None

        full_pool: set[Label] = set()
        reduced_pool: set[Label] = set()
        active_labels: set[Label] = set()
        total_members = 0
        total_active = 0
        total_examined = 0

        # Outsider groups in the coupled closure (not origins of the target
        # row): conservatively include all their member labels. Full-plan
        # recomputation decides whether each is truly needed; they cannot be
        # reduced by the origin's target values, and the shared label is the
        # only way they can participate in a minimal deletion set.
        for key in closure - origin_keys:
            full_pool |= label_sets[key]
            reduced_pool |= label_sets[key]

        # Per origin group: target values, member activity classification,
        # and the selective inert-label gate.
        from .provenance import MAX_RECOMPUTE_POOL

        for target_agg in origin_rows:
            key = target_agg["data"][:n_key]
            members = groups[key]
            total_members += len(members)
            target_values = dict(zip(out_schema, target_agg["data"]))

            active = [False] * len(members)
            for fn, col, _asname in aggs:
                target = target_values[_asname]
                if fn == "count":
                    if col is None or col == "*":
                        for i in range(len(members)):
                            active[i] = True
                    else:
                        ci = in_names.index(col)
                        for i, m in enumerate(members):
                            if m["data"][ci] is not None:
                                active[i] = True
                elif fn == "sum":
                    ci = in_names.index(col)
                    for i, m in enumerate(members):
                        v = m["data"][ci]
                        if v is not None and v != 0:
                            active[i] = True
                else:  # min / max
                    ci = in_names.index(col)
                    for i, m in enumerate(members):
                        v = m["data"][ci]
                        if v is not None and target is not None and v == target:
                            active[i] = True

            origin_full: set[Label] = set()
            origin_active: set[Label] = set()
            active_members: list[dict] = []
            inert_members: list[dict] = []
            for i, m in enumerate(members):
                member_labels: set[Label] = set()
                for w in m["witnesses"]:
                    member_labels.update(w)
                origin_full.update(member_labels)
                if active[i]:
                    origin_active.update(member_labels)
                    active_members.append(m)
                else:
                    inert_members.append(m)

            total_active += len(active_members)
            full_pool |= origin_full
            active_labels |= origin_active

            # A mixed-sign origin group necessarily has non-zero members.
            if not origin_active:
                reduced_pool |= origin_full
                continue

            if len(origin_active) > MAX_RECOMPUTE_POOL:
                # Gate cannot shrink an already-oversized active pool; the
                # caller raises the structured pool-too-large error.
                reduced_pool |= origin_active
                continue

            widened, examined = self._selective_inert_widening(
                aggs=aggs,
                in_names=in_names,
                target_values=target_values,
                active_members=active_members,
                inert_members=inert_members,
                active_pool=origin_active,
                full_pool=origin_full,
            )
            total_examined += examined
            reduced_pool |= widened

        # Defensive: a mixed-sign sum guarantees active members exist, so a
        # completely empty pool should not arise; leave the decision to the
        # caller rather than searching nothing.
        if not reduced_pool:
            return None

        return RecomputePool(
            full=full_pool,
            reduced=reduced_pool,
            group_member_count=total_members,
            active_member_count=total_active,
            active_labels=active_labels,
            gate_candidates=total_examined,
        )

    # -- selective inert-label widening (exact structural gate) -------------

    # Upper bound on active-label masks examined per target. The structural
    # analysis costs no plan recomputation; beyond the bound the gate falls
    # back to the conservative full member-label pool (which then has to fit
    # the 20-label enumeration cap, exactly like any reduced pool).
    _GATE_CANDIDATE_LIMIT = 200_000

    def _selective_inert_widening(
        self,
        *,
        aggs: list[tuple[str, str | None, str]],
        in_names: list[str],
        target_values: dict[str, Any],
        active_members: list[dict],
        inert_members: list[dict],
        active_pool: set[Label],
        full_pool: set[Label],
    ) -> tuple[set[Label], int]:
        """Decide which inert-only labels the exact search must enumerate.

        A label is inert-only when it occurs in no active member's witnesses.
        Such a label *z* belongs to an inclusion-minimal deletion set
        ``D = A ∪ Z`` (``A`` active labels, ``Z`` inert-only labels) exactly
        when there is an active mask ``A`` for which:

        1. deleting ``A`` removes **every** active member — otherwise the
           surviving active members alone already determine the aggregate
           values and the inert labels in ``D`` are redundant;
        2. the **inert members surviving ``A`` alone still reproduce the full
           target row** — otherwise the target is already gone after ``A``
           and again no inert label is needed;
        3. every surviving inert member has an ``A``-disjoint witness
           containing an inert-only label (some subset ``Z`` has to be able
           to kill them all);
        4. ``A`` is essential in at least one of its active labels: dropping
           that label must leave a group (resurrected active members plus the
           inert members then surviving) that *can still take the target
           values* — otherwise ``A`` itself is redundant for the emptiness
           kill and no minimal set has this active part; and
        5. *z* lies in some inclusion-minimal hitting set of the surviving
           inert members' inert-only witness edges (it must be able to carry
           its share of the kill without a dominating smaller edge).

        Conditions 1–4 are evaluated purely structurally: a labeled member
        survives a label deletion iff one of its witnesses is disjoint from
        the deleted labels, and aggregate values are simulated over the
        survivors. This is exact for scan/filter/project/join inputs and
        needs no whole-plan recomputation.

        Two witness-dependency cuts keep the mask enumeration tractable
        without ever changing the outcome:

        * **Irrelevance fast path** — when no non-empty subset of ALL inert
          members can reproduce the target row, condition 2 can never hold,
          so every inert-only label is provably irrelevant and the pool
          stays active-only at zero mask cost.
        * **Survivor-reproducibility pruning** — inert survivor sets only
          shrink as a mask grows, so once the inert members surviving some
          mask can no longer reproduce the target (in any non-empty
          subset), no superset of that mask can qualify either and the
          whole DFS subtree is pruned. This is what stops a shared label
          (e.g. the common left side of a self-join, present in every
          member witness) from exploding the enumeration: any mask
          containing it kills the dependent inert members too, and the
          2**k supersets of such a mask are skipped instead of counted
          against the mask budget.

        Returns ``(widened pool, masks examined)``.
        """
        # Local import: provenance imports evaluator types at module load.
        from .provenance import minimal_hitting_sets

        inert_only = full_pool - active_pool
        if not inert_only:
            return set(active_pool), 0

        target_tuple = tuple(target_values[a] for _, _, a in aggs)

        # Fast path: the inert-only labels are provably irrelevant when the
        # inert members cannot present the target row in any non-empty
        # subset, whatever the active mask deletes.
        if not self._inert_target_achievable(
            aggs, in_names, target_tuple, inert_members
        ):
            return set(active_pool), 0

        active_labels = sorted(active_pool, key=lambda lab: (lab[0], lab[1]))
        index = {lab: i for i, lab in enumerate(active_labels)}
        n = len(active_labels)

        def mask_of_witness(w: Witness) -> int:
            m = 0
            for lab in w:
                j = index.get(lab)
                if j is not None:
                    m |= 1 << j
            return m

        active_edges = [
            frozenset(mask_of_witness(w) for w in m["witnesses"])
            for m in active_members
        ]
        inert_edges = [
            (m, frozenset(mask_of_witness(w) for w in m["witnesses"]))
            for m in inert_members
        ]

        def survives(member_edges: frozenset[int], mask: int) -> bool:
            return any((e & mask) == 0 for e in member_edges)

        def survivor_rows(edges, mask):
            return [m for m, es in edges if survives(es, mask)]

        def kills_all_active(mask: int) -> bool:
            return all(not survives(es, mask) for es in active_edges)

        def agg_values(rows: list[dict]):
            """Aggregate tuple aligned to ``aggs``; None iff input is empty."""
            if not rows:
                return None
            out = []
            for fn, col, _ in aggs:
                if fn == "count":
                    if col is None or col == "*":
                        out.append(len(rows))
                    else:
                        ci = in_names.index(col)
                        out.append(sum(1 for r in rows if r["data"][ci] is not None))
                    continue
                ci = in_names.index(col)
                vals = [r["data"][ci] for r in rows if r["data"][ci] is not None]
                if fn == "sum":
                    out.append(sum(vals) if vals else None)
                elif fn == "min":
                    out.append(min(vals) if vals else None)
                else:
                    out.append(max(vals) if vals else None)
            return tuple(out)

        # Enumerate active masks that kill every active member. Every such
        # mask is a superset of at least one minimal hitting set of the
        # active witness edges, so for each minimal cover enumerate its
        # supersets by DFS (each mask is processed at most once), pruning
        # subtrees whose inert survivors can no longer reproduce the target,
        # with a hard bound on processed masks: beyond it the gate
        # conservatively widens to the full member-label pool.
        edge_label_sets: set[Witness] = set()
        for es in active_edges:
            for e in es:
                edge_label_sets.add(
                    frozenset(active_labels[j] for j in range(n) if e & (1 << j))
                )

        examined = 0
        overflow = False
        qualifying: set[int] = set()
        processed: dict[int, bool] = {}

        def examine(mask: int) -> bool:
            """Record *mask*; return True when the DFS must not grow beyond
            it (pruned subtree or budget overflow)."""
            nonlocal examined, overflow
            cached = processed.get(mask)
            if cached is not None:
                return cached
            examined += 1
            if examined > self._GATE_CANDIDATE_LIMIT:
                overflow = True
                processed[mask] = True
                return True
            survivors = survivor_rows(inert_edges, mask)
            # Survivor sets only shrink as the mask grows: when no non-empty
            # subset of the current survivors can reproduce the target row,
            # no superset of this mask can qualify — prune the subtree.
            if not self._inert_target_achievable(
                aggs, in_names, target_tuple, survivors
            ):
                processed[mask] = True
                return True
            # Conditions 1 and 2.
            if kills_all_active(mask) and agg_values(survivors) == target_tuple:
                qualifying.add(mask)
            processed[mask] = False
            return False

        # Deterministic order of minimal covers for stable diagnostics.
        minimal_covers = sorted(
            minimal_hitting_sets(edge_label_sets),
            key=lambda cover: sorted((lab[0], lab[1]) for lab in cover),
        )
        for cover in minimal_covers:
            base = 0
            for lab in cover:
                base |= 1 << index[lab]

            def grow(mask: int, start: int) -> None:
                if examine(mask) or overflow:
                    return
                for j in range(start, n):
                    bit = 1 << j
                    if mask & bit:
                        continue
                    grow(mask | bit, j + 1)
                    if overflow:
                        return

            grow(base, 0)
            if overflow:
                break

        if overflow:
            # Structural analysis could not finish; conservatively widen to
            # the full member-label pool (still subject to the cap upstream).
            return set(full_pool), examined

        if not qualifying:
            return set(active_pool), examined

        widened: set[Label] = set(active_pool)
        for mask in qualifying:
            # Condition 4: at least one active bit must be essential. With
            # that bit dropped, the group (resurrected active members plus
            # every inert member that then survives) must be able to take the
            # target values while staying non-empty.
            essential = False
            for j in range(n):
                bit = 1 << j
                if not (mask & bit):
                    continue
                reduced = mask & ~bit
                resurrected = [
                    m
                    for k, m in enumerate(active_members)
                    if survives(active_edges[k], reduced)
                ]
                inert_surv = survivor_rows(inert_edges, reduced)
                if self._target_reproducible(
                    aggs, in_names, target_tuple, resurrected, inert_surv
                ):
                    essential = True
                    break
            if not essential:
                continue

            # Condition 3: the inert members surviving A must all be killable
            # by inert-only labels. Such a member is killed iff EVERY one of
            # its A-disjoint witnesses is hit; if any such witness still
            # contains an active label (shared with an active member but not
            # selected in A), no inert-only Z can remove the member and this
            # active part cannot occur in a minimal deletion set. Otherwise
            # the flat family of those witnesses is exactly what Z must
            # transversal-hit.
            required_edges: set[Witness] = set()
            ruled_out = False
            for m in survivor_rows(inert_edges, mask):
                disjoint = [
                    w for w in m["witnesses"] if (mask_of_witness(w) & mask) == 0
                ]
                if any(not w <= inert_only for w in disjoint):
                    ruled_out = True
                    break
                required_edges |= set(disjoint)
            if ruled_out or not required_edges:
                continue

            # Condition 5: restore the inert-only labels that occur in some
            # inclusion-minimal hitting set of the required edges.
            for z in inert_only:
                if label_can_hit_minimally(z, frozenset(required_edges)):
                    widened.add(z)
        return widened, examined

    @staticmethod
    def _inert_target_achievable(
        aggs: list[tuple[str, str | None, str]],
        in_names: list[str],
        target_tuple: tuple,
        survivors: list[dict],
    ) -> bool:
        """Whether some NON-EMPTY subset of *survivors* (all inert members)
        can still take every target aggregate value.

        Inert members carry zero/NULL values (sum, count(col)), non-extremal
        values (min/max), or count(*) padding, so the values a subset can
        produce are tightly limited and checked exactly per aggregate:

        * ``count(*)``: any size 1..|survivors|;
        * ``count(col)``: always 0 (inert members are NULL in ``col``);
        * ``sum(col)``: 0 when a zero-valued member is kept, NULL when only
          NULL members are kept — never a non-zero number;
        * ``min``/``max(col)``: any non-NULL inert value (a singleton
          subset), or NULL from an all-NULL subset.

        This is a sound pruning criterion for the mask DFS: inert survivor
        sets only shrink as the deletion mask grows, so if no non-empty
        subset of the current survivors can reproduce the target row, no
        superset of the current mask can satisfy the gate's condition 2.
        """
        if not survivors:
            return False
        n = len(survivors)
        for k, (fn, col, _) in enumerate(aggs):
            target = target_tuple[k]
            if fn == "count":
                if col is None or col == "*":
                    if not (1 <= target <= n):
                        return False
                elif target != 0:
                    return False
                continue
            ci = in_names.index(col)
            vals = [r["data"][ci] for r in survivors]
            non_null = [v for v in vals if v is not None]
            any_null = len(non_null) != len(vals)
            if fn == "sum":
                if target is None:
                    if not any_null:
                        return False
                elif target != 0 or not any(v == 0 for v in non_null):
                    return False
            else:  # min / max
                if target is None:
                    if not any_null:
                        return False
                elif target not in non_null:
                    return False
        return True

    @staticmethod
    def _target_reproducible(
        aggs: list[tuple[str, str | None, str]],
        in_names: list[str],
        target_tuple: tuple,
        resurrected_active: list[dict],
        inert_survivors: list[dict],
    ) -> bool:
        """Whether keeping every resurrected active member and an arbitrary
        subset of the inert survivors lets the group take the target values.

        Inert members are zero/NULL (sum, count(col)), non-extremal
        (min/max), or count(*) padding, so per aggregate this is a simple
        value/range check over the resurrected active values.
        """
        if not resurrected_active and not inert_survivors:
            return False
        for k, (fn, col, _) in enumerate(aggs):
            target = target_tuple[k]
            if fn == "count":
                if col is None or col == "*":
                    lo = len(resurrected_active)
                    hi = lo + len(inert_survivors)
                else:
                    ci = in_names.index(col)
                    lo = sum(
                        1 for r in resurrected_active if r["data"][ci] is not None
                    )
                    hi = lo + sum(
                        1 for r in inert_survivors if r["data"][ci] is not None
                    )
                if not (lo <= target <= hi):
                    return False
                continue
            ci = in_names.index(col)
            av = [r["data"][ci] for r in resurrected_active if r["data"][ci] is not None]
            iv = [r["data"][ci] for r in inert_survivors if r["data"][ci] is not None]
            if fn == "sum":
                # Inert non-NULL values are 0, so they never move the sum.
                if (sum(av) if av else None) != target:
                    return False
            elif fn == "min":
                if target is None or not av or min(av) != target:
                    return False
                # Inert values are non-extremal; none may go below target.
                if iv and min(iv) < target:
                    return False
            else:  # max
                if target is None or not av or max(av) != target:
                    return False
                if iv and max(iv) > target:
                    return False
        return True

    def _agg_spec(self, node: dict):
        aggs = node.get("aggregates", [])
        if not isinstance(aggs, list) or not aggs:
            raise ApiError(
                "MALFORMED_PLAN", "aggregate node needs a non-empty 'aggregates' list"
            )
        norm = []
        for a in aggs:
            if not isinstance(a, dict) or not isinstance(a.get("fn"), str):
                raise ApiError("MALFORMED_PLAN", "aggregate entries need string 'fn'")
            fn = a["fn"]
            if fn not in self._AGG_FNS:
                raise ApiError(
                    "MALFORMED_PLAN",
                    f"Unsupported aggregate {fn!r}; allowed: count/sum/min/max",
                )
            col = a.get("column")
            if fn != "count" and not isinstance(col, str):
                raise ApiError(
                    "MALFORMED_PLAN", f"{fn} aggregate requires a string 'column'"
                )
            if fn == "count" and col is not None and not isinstance(col, str):
                raise ApiError(
                    "MALFORMED_PLAN", "count column must be a string or '*'"
                )
            asname = a.get("as")
            if not isinstance(asname, str) or not asname:
                raise ApiError("MALFORMED_PLAN", "aggregate requires string 'as'")
            norm.append((fn, col, asname))
        names = [a[2] for a in norm]
        if len(set(names)) != len(names):
            raise ApiError("MALFORMED_PLAN", "aggregate output aliases must be unique")
        return norm

    def _agg_schema(self, node: dict) -> list[str]:
        gb = node.get("group_by", [])
        if not isinstance(gb, list) or not all(isinstance(c, str) for c in gb):
            raise ApiError("MALFORMED_PLAN", "aggregate 'group_by' must be strings")
        out = list(gb) + [a[2] for a in self._agg_spec(node)]
        if len(set(out)) != len(out):
            raise ApiError(
                "MALFORMED_PLAN", "aggregate output column names must be unique"
            )
        return out

    def _agg_component(
        self, fn: str, col: str | None, names: list[str], members: list[dict]
    ) -> tuple[Any, set[Witness]]:
        """Value and witness family of one aggregate expression over a group.

        The family characterises the member-row sets that still produce the
        value, so that the minimal deletion sets (hitting sets of the row's
        witnesses) are exactly replayed by recomputation:

        * ``count(*)`` / ``count(col)`` covers every row participating in the
          count (all members, resp. all members with a non-NULL value): one
          conjunction over those rows;
        * ``sum(col)`` enumerates the inclusion-minimal NON-EMPTY row
          combinations whose values add up to the target sum;
        * ``min(col)`` / ``max(col)`` enumerates the inclusion-minimal
          combinations still yielding the extremum — one per member row equal
          to it (equal-valued rows give distinct combinations).

        NULLs never participate in ``count(col)``/``sum``/``min``/``max``; an
        all-NULL aggregate (value NULL) constrains nothing beyond group
        existence and contributes the trivial family ``{∅}``.
        """
        if fn == "count":
            if col is None or col == "*":
                counted = list(members)
            else:
                ci = names.index(col)
                counted = [m for m in members if m["data"][ci] is not None]
            return len(counted), cover_witnesses(counted)

        ci = names.index(col)
        vals = [(m, m["data"][ci]) for m in members if m["data"][ci] is not None]
        if fn == "sum":
            bad = [v for _, v in vals if not is_number(v)]
            if bad:
                raise ApiError(
                    "TYPE_ERROR",
                    f"sum({col!r}): non-numeric value "
                    f"{bad[0]!r} ({type_name(bad[0])})",
                    column=col,
                )
            if not vals:
                return None, {frozenset()}
            target = 0
            for _, v in vals:
                target += v
            family: set[Witness] = set()
            for combo in minimal_sum_combos(vals, target):
                family |= cover_witnesses(combo)
            return target, prune(family)

        # min / max
        kinds = {type_name(v) for _, v in vals}
        non_numeric = kinds - {"number"}
        if non_numeric and ({"number"} & kinds or len(non_numeric) > 1):
            raise ApiError(
                "TYPE_ERROR",
                f"{fn}({col!r}): cannot order mixed types {sorted(kinds)}",
                column=col,
            )
        if not vals:
            return None, {frozenset()}
        target = (min if fn == "min" else max)(v for _, v in vals)
        family = set()
        for m, v in vals:
            if v == target:
                family |= m["witnesses"]
        return target, prune(family)

    def _aggregate(self, node: dict) -> list[dict]:
        src_node = self._input(node)
        gb = node.get("group_by", [])
        aggs = self._agg_spec(node)
        names = self.schema(src_node)

        for c in gb:
            if c not in names:
                raise ApiError(
                    "ILLEGAL_REFERENCE",
                    f"group_by references unknown column {c!r}",
                    column=c,
                    available=names,
                )
        for fn, col, asname in aggs:
            if fn != "count" and col not in names:
                raise ApiError(
                    "ILLEGAL_REFERENCE",
                    f"aggregate {asname!r} references unknown column {col!r}",
                    column=col,
                    available=names,
                )
            if fn == "count" and col not in (None, "*") and col not in names:
                raise ApiError(
                    "ILLEGAL_REFERENCE",
                    f"aggregate {asname!r} references unknown column {col!r}",
                    column=col,
                    available=names,
                )

        src = self.eval(src_node)

        if not src:
            # Aggregating an empty relation is always rejected: for grouped
            # aggregates it would silently yield zero groups, for scalar
            # aggregates a single row of NULL/0 — the client must distinguish
            # "no input" explicitly.
            raise ApiError(
                "EMPTY_AGGREGATE",
                "aggregate input is empty: no groups can be formed",
            )

        gi = [names.index(c) for c in gb]

        # NULL group keys form their own group. We key the dict with a
        # sentinel-prefixed tuple so a leading literal value can never collide
        # with the marker, and restore the real key when emitting.
        NULL_KEY = object()
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for r in src:
            gkey = tuple(r["data"][i] for i in gi)
            if any(k is None for k in gkey):
                groups[(NULL_KEY,) + gkey].append(r)
            else:
                groups[gkey].append(r)

        out_rows = []
        for gkey, members in groups.items():
            real_key = gkey[1:] if gkey and gkey[0] is NULL_KEY else gkey
            data: list[Any] = list(real_key)
            # Witness components that must all hold for this exact output row:
            #   1. group existence — at least one member row survives;
            #   2. one component per aggregate expression pinning its value.
            # The row's witnesses are the conjunction (pairwise union) of the
            # component families, pruned to inclusion-minimal sets.
            families: list[set[Witness]] = []
            existence: set[Witness] = set()
            for m in members:
                existence.update(m["witnesses"])
            families.append(prune(existence))
            for fn, col, asname in aggs:
                value, family = self._agg_component(fn, col, names, members)
                data.append(value)
                families.append(family)
            witnesses: set[Witness] = {frozenset()}
            for family in families:
                witnesses = prune({a | b for a in witnesses for b in family})
            out_rows.append({"data": tuple(data), "witnesses": witnesses})
        return merge_relation(out_rows)
