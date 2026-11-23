"""Small SQLite helpers.

SQLite caps the number of bound parameters per statement (traditionally 999),
so anything that expands a Python collection into ``IN (...)`` must chunk.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

MAX_PARAMS = 900


def chunks(values: Iterable, size: int = MAX_PARAMS) -> Iterator[list]:
    batch: list = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))
