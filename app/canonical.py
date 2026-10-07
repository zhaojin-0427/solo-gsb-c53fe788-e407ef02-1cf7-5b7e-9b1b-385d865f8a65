"""Canonical JSON: byte-stable serialization used for every API response.

Rules:
  * dict keys are emitted in lexicographic (UTF-8 code point) order;
  * list order is preserved (it carries semantics);
  * only JSON-native scalar types are accepted;
  * no insignificant whitespace, no trailing newline.
"""
from __future__ import annotations

import json
from typing import Any


def canonicalize(obj: Any) -> Any:
    """Normalize an already-JSON-compatible value into canonical layout."""
    if isinstance(obj, dict):
        return {k: canonicalize(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [canonicalize(v) for v in obj]
    return obj


class CanonicalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # pragma: no cover - defensive
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    def encode(self, o: Any) -> str:
        return super().encode(canonicalize(o))

    def iterencode(self, o: Any, _one_shot: bool = False):  # type: ignore[override]
        return super().iterencode(canonicalize(o), _one_shot=_one_shot)


def dumps(obj: Any) -> bytes:
    """Serialize *obj* to canonical JSON bytes."""
    return json.dumps(
        canonicalize(obj),
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        cls=None,
    ).encode("utf-8")


def fingerprint(obj: Any) -> str:
    """Stable digest of the canonical encoding of *obj*."""
    import hashlib

    return hashlib.sha256(dumps(obj)).hexdigest()
