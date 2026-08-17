"""Shared service-layer exception: field -> message(s), so a form can show
them inline. Domain-specific subclasses exist only for readability at the
call site (``except RegistrationError`` reads better than a generic catch).
"""

from __future__ import annotations


class ValidationError(Exception):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__("validation failed")
        self.errors = errors
