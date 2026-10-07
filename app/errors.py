"""Structured error type shared across the application."""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error that must be reported to the client as structured JSON."""

    def __init__(self, code: str, message: str, status_code: int = 400, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }
        if self.details:
            body["error"]["details"] = self.details
        return body
