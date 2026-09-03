"""Shared CD error type. CLI maps it to an exit code."""

from __future__ import annotations


class CdError(Exception):
    def __init__(self, msg: str, code: int = 1) -> None:
        super().__init__(msg)
        self.code = code
