# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Memory as the demo APIs expose it: the store a deployment keeps facts in, the fixture
seeder, and the read/retract routes both roles mount. Facts are keyed by the principal the
session id resolves to, and a fact key travels in the request body so it never appears in
a URL or an access log."""

# Route parameters below are annotated with dependencies built at call time, so this
# module evaluates its annotations eagerly (no ``from __future__ import annotations``).

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from commerce_common.memory import (
    InMemoryMemoryStore,
    JsonFileMemoryStore,
    MemoryStore,
    match_facts,
)
from commerce_common.types import MemoryFact

from .state import StateStore, deployment_state_store


class MemoryFactRef(BaseModel):
    key: str = Field(min_length=1, max_length=64)


class MemoryFactEdit(MemoryFactRef):
    value: str = Field(min_length=1, max_length=200)


class DurableMemoryStore:
    """``MemoryStore`` over the deployment's shared store: one field map per subject, and
    a counter for its purge generation. What a shopper asks the store to remember, or to
    forget, then outlives the process that heard it — which is the point of memory, and
    what a file beside the fixtures cannot do where the filesystem is read-only."""

    def __init__(self, store: StateStore, *, prefix: str) -> None:
        self._store = store
        self._prefix = prefix.rstrip(":")

    def _facts_key(self, subject_id: str) -> str:
        return f"{self._prefix}:memory:{subject_id}"

    def _purges_key(self, subject_id: str) -> str:
        return f"{self._prefix}:memory:{subject_id}:purges"

    async def get_facts(self, subject_id: str) -> list[MemoryFact]:
        stored = await self._store.read_map(self._facts_key(subject_id))
        return [MemoryFact.model_validate(entry) for entry in stored.values()]

    async def upsert_facts(self, subject_id: str, facts: list[MemoryFact]) -> None:
        if facts:
            await self._store.write_map(
                self._facts_key(subject_id),
                {fact.key: fact.model_dump(mode="json") for fact in facts},
            )

    async def search_facts(self, subject_id: str, query: str) -> list[MemoryFact]:
        return match_facts(await self.get_facts(subject_id), query)

    async def delete_fact(self, subject_id: str, key: str) -> bool:
        return await self._store.delete_map_field(self._facts_key(subject_id), key)

    async def clear(self, subject_id: str) -> None:
        await self._store.delete(self._facts_key(subject_id))
        await self._store.increment(self._purges_key(subject_id))

    async def purge_generation(self, subject_id: str) -> int:
        return await self._store.read_counter(self._purges_key(subject_id))


def memory_store_for(
    *, prefix: str, local: MemoryStore | None = None, store: StateStore | None = None
) -> MemoryStore:
    """The memory store for one role: the deployment's shared one when the environment
    names a state store, and ``local`` (an in-process store, or the retail example's file
    beside its fixtures) otherwise."""
    resolved = store if store is not None else deployment_state_store()
    if resolved is None:
        return local if local is not None else InMemoryMemoryStore()
    return DurableMemoryStore(resolved, prefix=prefix)


def json_file_memory_store(path: Path) -> MemoryStore:
    """The file-backed store when its directory can be written, an in-process one when it
    cannot: a deployed function's bundle is read-only, and a store whose writes raise is
    worse than one that does not outlive the process. A deployment with a state store gets
    neither of these (``memory_store_for`` above)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        return InMemoryMemoryStore()
    return JsonFileMemoryStore(path)


class SeedMarker(Protocol):
    """Which subjects a store has already been seeded for."""

    async def seeded_users(self) -> set[str]: ...

    async def mark_seeded(self, user_id: str) -> None: ...


class FileSeedMarker:
    """The marker beside the fixtures, for a local run whose store is a file too."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def seeded_users(self) -> set[str]:
        if not self._path.exists():
            return set()
        return set(json.loads(self._path.read_text(encoding="utf-8") or "[]"))

    async def mark_seeded(self, user_id: str) -> None:
        users = await self.seeded_users() | {user_id}
        # A read-only bundle cannot hold the marker; the seed then reloads next boot,
        # which is what a store with no marker does anyway.
        with contextlib.suppress(OSError):
            self._path.write_text(json.dumps(sorted(users)), encoding="utf-8")


class StateSeedMarker:
    """The marker in the deployment's shared store, so the first process to seed a
    subject is the only one that does, however many processes boot."""

    def __init__(self, store: StateStore, *, prefix: str) -> None:
        self._store = store
        self._key = f"{prefix.rstrip(':')}:memory:seeded"

    async def seeded_users(self) -> set[str]:
        return set(await self._store.read_members(self._key))

    async def mark_seeded(self, user_id: str) -> None:
        await self._store.add_member(self._key, user_id)


class MemorySeeder:
    """Loads ``data/memory-seed.json`` (``{user_id: [facts]}``) into a store.

    Without a marker every boot reloads the seed, which suits an in-memory store.
    With one, each user is seeded once and the marker remembers who; the store is then
    the only source of truth across restarts, so a retracted fact stays retracted and a
    purged user stays empty until ``reseed`` is asked for explicitly. ``marker`` takes a
    path (the local file) or any ``SeedMarker``; ``seed_marker`` below picks the one a
    deployment needs.
    """

    def __init__(self, seed_file: Path, marker: Path | SeedMarker | None = None) -> None:
        self._seed_file = seed_file
        self._marker: SeedMarker | None = (
            FileSeedMarker(marker) if isinstance(marker, Path) else marker
        )

    def _entries(self) -> dict[str, list[dict[str, Any]]]:
        if not self._seed_file.exists():
            return {}
        return json.loads(self._seed_file.read_text(encoding="utf-8"))

    async def _load(self, store: MemoryStore, user_id: str, entries: list[dict[str, Any]]) -> None:
        # Seed entries carry their own updated_at (check.py enforces it); the stamp here
        # only backstops a hand-written seed so retention has something to compare.
        now = datetime.now(UTC).isoformat()
        facts = [MemoryFact.model_validate({"updated_at": now, **entry}) for entry in entries]
        await store.upsert_facts(user_id, facts)
        if self._marker is not None:
            await self._marker.mark_seeded(user_id)

    async def seed_at_boot(self, store: MemoryStore) -> None:
        already = await self._marker.seeded_users() if self._marker is not None else set()
        for user_id, entries in self._entries().items():
            if user_id not in already:
                await self._load(store, user_id, entries)

    async def reseed(self, store: MemoryStore, user_id: str) -> None:
        entries = self._entries().get(user_id)
        if entries is not None:
            await self._load(store, user_id, entries)


def seed_marker(
    path: Path | None, *, prefix: str, store: StateStore | None = None
) -> Path | SeedMarker | None:
    """The marker a vertical's seeder uses: the shared store's when the environment names
    one, ``path`` otherwise (``None`` where the vertical reloads its seed every boot)."""
    resolved = store if store is not None else deployment_state_store()
    if resolved is not None:
        return StateSeedMarker(resolved, prefix=prefix)
    return path


def install_memory_routes(
    target: FastAPI | APIRouter,
    path: str,
    *,
    current_session: Any,
    memory_store: MemoryStore,
) -> None:
    """``GET path`` lists the caller's facts and ``DELETE path`` retracts one of them."""

    @target.get(path)
    async def get_memory(record: current_session) -> dict:
        facts = await memory_store.get_facts(record.user_id)
        return {"facts": [fact.model_dump(mode="json") for fact in facts]}

    @target.delete(path)
    async def delete_memory_fact(ref: MemoryFactRef, record: current_session) -> dict:
        if not await memory_store.delete_fact(record.user_id, ref.key):
            raise HTTPException(status_code=404, detail="No such fact")
        return {"ok": True, "deleted": ref.key}
