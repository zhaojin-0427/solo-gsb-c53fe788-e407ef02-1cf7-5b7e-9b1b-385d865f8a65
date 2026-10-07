"""Scalar value helpers: typing, 3-valued predicate logic and canonical order.

All values originate from JSON, so the only runtime types are
None / bool / int / float / str.
"""
from __future__ import annotations

from typing import Any

from .errors import ApiError

# Ordering rank used for canonical tuple/witness sorting.
_RANK = {type(None): 0, bool: 1, int: 2, float: 2, str: 3, dict: 4, list: 5}


def type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    return type(v).__name__


def type_rank(v: Any) -> int:
    return _RANK.get(type(v), 9)


def value_sort_key(v: Any):
    """Total order over JSON scalars: null < bool < number < string."""
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (2, float(v))
    if isinstance(v, str):
        return (3, v)
    raise ApiError("TYPE_ERROR", f"Cannot order value of type {type_name(v)}")


def tuple_sort_key(t: tuple):
    return tuple(value_sort_key(v) for v in t)


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def strict_equal(a: Any, b: Any) -> bool | None:
    """Equality with SQL-ish semantics.

    Returns None when either side is NULL. Equality across incompatible
    scalar types is False (e.g. ``1 = "1"``, ``true = 1``).
    """
    if a is None or b is None:
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_number(a) and is_number(b):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return False


def compare(a: Any, op: str, b: Any) -> bool | None:
    """Scalar comparison; returns None (UNKNOWN) on NULL input."""
    if a is None or b is None:
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        raise ApiError(
            "TYPE_ERROR",
            f"Ordered comparison is not defined for booleans ({a!r} {op} {b!r})",
        )
    if is_number(a) and is_number(b):
        pass
    elif isinstance(a, str) and isinstance(b, str):
        pass
    else:
        raise ApiError(
            "TYPE_ERROR",
            f"Cannot compare values of mismatched types {type_name(a)} and "
            f"{type_name(b)}",
        )
    if op == "=":
        return a == b
    if op == "<>":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    raise ApiError("MALFORMED_PLAN", f"Unknown comparison operator {op!r}")


# ---------------------------------------------------------------------------
# Predicate trees (3-valued logic). A predicate is one of:
#   {"and": [pred, ...]} | {"or": [pred, ...]} | {"not": pred}
#   {"column": "c", "op": "=", "value": 1}
#   {"column": "c", "op": "is_null"}
# ---------------------------------------------------------------------------

_COMPARISON_OPS = {"=", "<>", "<", "<=", ">", ">="}


def eval_predicate(pred: dict, row: dict) -> bool | None:
    if not isinstance(pred, dict) or not pred:
        raise ApiError(
            "MALFORMED_PLAN",
            "Predicate must be a non-empty object with key "
            "'and'/'or'/'not'/'column'",
        )
    compound = [k for k in pred if k in ("and", "or", "not")]
    if compound:
        if len(pred) != 1:
            raise ApiError(
                "MALFORMED_PLAN",
                f"Compound predicate {compound[0]!r} must have no sibling keys",
            )
        key, body = compound[0], pred[compound[0]]
    elif "column" in pred:
        key, body = "column", pred
    else:
        raise ApiError(
            "MALFORMED_PLAN",
            "Predicate must have key 'and'/'or'/'not'/'column'",
        )
    if key == "and":
        result: bool | None = True
        for p in body:
            r = eval_predicate(p, row)
            if r is False:
                return False
            if r is None:
                result = None
        return result
    if key == "or":
        result = False
        for p in body:
            r = eval_predicate(p, row)
            if r is True:
                return True
            if r is None:
                result = None
        return result
    if key == "not":
        r = eval_predicate(body, row)
        if r is None:
            return None
        return not r
    if key == "column":
        col = body.get("column")
        op = body.get("op")
        if not isinstance(col, str) or not isinstance(op, str):
            raise ApiError("MALFORMED_PLAN", "Predicate needs string 'column' and 'op'")
        if col not in row:
            raise ApiError(
                "ILLEGAL_REFERENCE",
                f"Predicate references unknown column {col!r}",
                column=col,
            )
        v = row[col]
        if op == "is_null":
            return v is None
        if op == "is_not_null":
            return v is not None
        if op not in _COMPARISON_OPS:
            raise ApiError("MALFORMED_PLAN", f"Unknown predicate operator {op!r}")
        if "value" not in body:
            raise ApiError("MALFORMED_PLAN", f"Predicate {op!r} requires a 'value'")
        lit = body["value"]
        if not isinstance(lit, (str, int, float, bool)) and lit is not None:
            raise ApiError(
                "MALFORMED_PLAN", "Predicate literal must be a JSON scalar or null"
            )
        return compare(v, op, lit)
    raise ApiError("MALFORMED_PLAN", f"Unknown predicate node {key!r}")
