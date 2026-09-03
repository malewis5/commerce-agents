# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""The mock world of one deployment, as a document its processes share.

The mock backends hold two different things. The fixtures in ``data/`` are code: every
process loads the same catalog, the same profiles, the same 90 days of metrics. What a
demo then *does* is not: a cart line, a staged change the operator has not approved yet,
a restock that moved a number, an eight-minute ticket hold. Locally those live in the one
process the demo runs. A deployment runs several, so they live here instead — one JSON
document per deployment, read before a request reaches a backend and written back after
the response has gone out, so the portal's Approve button finds the change the assistant
staged and the storefront shows the price the portal moved.

A mock declares its mutable state part by part (``WorldState``), naming for each part how
it is dumped and how it is put back over freshly loaded fixtures. Only the difference from
the fixtures travels for catalog records, so the document stays small: it is the demo's
edits, not its data.

``DurableWorld`` carries the document. With no state store it does nothing at all, which
is a local run: the process is the world, exactly as before. The middleware is plain ASGI
rather than a Starlette ``BaseHTTPMiddleware`` so that a streamed chat turn is written
back when its stream ends, not when its first byte leaves.

What this is not: a database. One document with a compare-and-set is right for a demo
world that one person is driving and wrong for a real catalog, where each system of record
holds its own rows and the backend methods call them.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import logging
from collections.abc import Callable, Mapping, MutableMapping, MutableSequence, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from .state import StateConflict, StateStore

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class WorldState:
    """The mutable state one mock backend holds, declared part by part.

    Each part is a pair: ``dump`` returns JSON, ``load`` puts JSON back. The helpers cover
    the four shapes the mocks use — a plain container they mutate in place, a map of
    pydantic records, a map of maps of them, and the handful of fields on a fixture record
    that a merchant change moves.
    """

    def __init__(self) -> None:
        self._parts: dict[str, tuple[Callable[[], Any], Callable[[Any], None]]] = {}

    # -- Declaring parts

    def part(self, name: str, *, dump: Callable[[], Any], load: Callable[[Any], None]) -> None:
        if name in self._parts:
            raise ValueError(f"world state already has a part named {name!r}")
        self._parts[name] = (dump, load)

    def container(self, name: str, target: MutableMapping | MutableSequence) -> None:
        """A JSON dict or list the mock reads and writes in place (an inventory overlay, a
        promotion window, a list of cohorts). Replaced in place on load, so anything
        holding a reference to it sees the new contents."""

        def load(document: Any) -> None:
            _replace_in_place(target, document)

        self.part(name, dump=lambda: copy.deepcopy(target), load=load)

    def models(self, name: str, target: MutableMapping[str, Any], model: type[ModelT]) -> None:
        """A map of pydantic records keyed by id (campaigns, staged changes)."""

        def dump() -> Any:
            return {key: record.model_dump(mode="json") for key, record in target.items()}

        def load(document: Any) -> None:
            target.clear()
            target.update(
                {key: model.model_validate(entry) for key, entry in (document or {}).items()}
            )

        self.part(name, dump=dump, load=load)

    def nested_models(
        self, name: str, target: MutableMapping[str, MutableMapping[str, Any]], model: type[ModelT]
    ) -> None:
        """A map of maps of pydantic records (cart lines, keyed by session then product)."""

        def dump() -> Any:
            return {
                outer: {key: record.model_dump(mode="json") for key, record in inner.items()}
                for outer, inner in target.items()
            }

        def load(document: Any) -> None:
            target.clear()
            for outer, inner in (document or {}).items():
                target[outer] = {
                    key: model.model_validate(entry) for key, entry in (inner or {}).items()
                }

        self.part(name, dump=dump, load=load)

    def value(self, name: str, get: Callable[[], Any], set_: Callable[[Any], None]) -> None:
        """One scalar (a ledger's sequence number, a counter)."""
        self.part(name, dump=get, load=set_)

    def overlay(self, name: str, records: Mapping[str, Any], fields: Sequence[str]) -> None:
        """The fields of fixture records that an applied change moves — a price, a stock
        flag, the attribute chips, listing copy.

        The fixture value of each field is whatever the record holds when the part is
        declared, so a mock declares its state where it finishes loading its fixtures and
        ``world_state`` is memoized (``declared_once``). Only records that differ from
        those values travel, and a record the document does not mention goes back to
        them. The mapping may name one object twice (a family and its variants both
        resolve by id); writing fields rather than replacing records keeps that sharing.
        """
        pristine = {
            record_id: {field: copy.deepcopy(getattr(record, field)) for field in fields}
            for record_id, record in records.items()
        }

        def dump() -> Any:
            changed: dict[str, dict[str, Any]] = {}
            for record_id, record in records.items():
                moved = {
                    field: _jsonable(getattr(record, field))
                    for field in fields
                    if getattr(record, field) != pristine[record_id][field]
                }
                if moved:
                    changed[record_id] = moved
            return changed

        def load(document: Any) -> None:
            overrides = document or {}
            for record_id, record in records.items():
                for field, value in pristine[record_id].items():
                    if getattr(record, field) != value:
                        setattr(record, field, copy.deepcopy(value))
                for field, value in (overrides.get(record_id) or {}).items():
                    if field in pristine[record_id]:
                        setattr(record, field, copy.deepcopy(value))

        self.part(name, dump=dump, load=load)

    def include(self, other: WorldState, *, prefix: str) -> None:
        """Fold another mock's parts in under a prefix (a merchant's, into its
        storefront's)."""
        for name, pair in other._parts.items():
            self.part(f"{prefix}.{name}", dump=pair[0], load=pair[1])

    # -- Using them

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._parts)

    def snapshot(self) -> dict[str, Any]:
        return {name: dump() for name, (dump, _) in self._parts.items()}

    def restore(self, document: Mapping[str, Any]) -> None:
        """Put a document back. A part the document does not name is reset to its
        fixtures, so a process never keeps state the document has dropped."""
        for name, (_, load) in self._parts.items():
            load(document.get(name))


def declared_once(build: Callable[[Any], WorldState]) -> Callable[[Any], WorldState]:
    """Decorator for a mock's ``world_state``: build it the first time it is asked for
    and keep it. A catalog overlay reads the fixture values off the records as it is
    declared, so two declarations of one mock would disagree about what the fixtures
    were; one, taken before a demo has done anything, is the whole point."""

    @functools.wraps(build)
    def declared(self: Any) -> WorldState:
        state = getattr(self, "_declared_world_state", None)
        if state is None:
            state = build(self)
            self._declared_world_state = state
        return state

    return declared


def _replace_in_place(target: Any, document: Any) -> None:
    if isinstance(target, MutableMapping):
        target.clear()
        target.update(copy.deepcopy(document or {}))
    elif isinstance(target, MutableSequence):
        del target[:]
        target.extend(copy.deepcopy(document or []))
    else:  # pragma: no cover - a declaration error, not a runtime one
        raise TypeError(f"container part needs a dict or a list, got {type(target)!r}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class DurableWorld:
    """The world document of one deployment.

    ``load`` is called before a request reaches the app and ``save`` after its response has
    been sent. The first request in flight owns the in-process world: it reads the document
    and, when the stored version is not the one this process already reflects, restores it.
    Requests that arrive while it is still running skip both, because the process's own
    state is by then the newest there is — a poll for the cart while a turn streams must not
    put the turn's writes back to what they were. The last one out writes, and only when the
    snapshot differs from what was loaded, so reads cost one document read and nothing else.

    A write whose version has moved means another process wrote between this request's read
    and its write. There is no merge for that: the document is one world. The later write
    wins and says so in the log; a demo is one person driving one deployment, and the case
    that matters (two processes serving one browser) is a version that has not moved.
    """

    def __init__(self, store: StateStore | None, key: str) -> None:
        self._store = store
        self._key = key
        self._state = WorldState()
        self._lock = asyncio.Lock()
        self._in_flight = 0
        self._version = 0
        self._loaded: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def include(self, backend: object, *, prefix: str) -> None:
        """Fold in a backend's declared state, if it declares any. A mock with nothing
        durable (or a vertical that has not declared its state yet) is simply absent from
        the document."""
        state = getattr(backend, "world_state", None)
        if state is None:
            return
        self._state.include(state(), prefix=prefix)

    async def load(self) -> None:
        if self._store is None:
            return
        async with self._lock:
            self._in_flight += 1
            if self._in_flight > 1:
                return
            try:
                stored = await self._store.read_document(self._key)
            except Exception:  # a store outage must not take the demo down
                logger.exception("world: read failed; serving this process's own state")
                self._loaded = None
                return
            if stored is not None and stored[0] != self._version:
                self._state.restore(stored[1])
                self._version = stored[0]
            elif stored is None:
                self._version = 0
            self._loaded = self._state.snapshot()

    async def save(self) -> None:
        if self._store is None:
            return
        async with self._lock:
            self._in_flight -= 1
            if self._in_flight > 0 or self._loaded is None:
                return
            document = self._state.snapshot()
            if document == self._loaded:
                return
            try:
                self._version = await self._store.write_document(self._key, document, self._version)
            except StateConflict:
                logger.warning("world: another process wrote first; this write wins")
                if not await self._overwrite(self._store, document):
                    return
            except Exception:
                logger.exception("world: write failed; this process keeps its own state")
                return
            self._loaded = document

    async def _overwrite(self, store: StateStore, document: dict[str, Any]) -> bool:
        """Write again from the version that beat us. True when it landed."""
        try:
            stored = await store.read_document(self._key)
            self._version = await store.write_document(
                self._key, document, stored[0] if stored else 0
            )
        except Exception:
            logger.exception("world: write failed twice; this process keeps its own state")
            return False
        return True


class WorldMiddleware:
    """Plain ASGI, so ``save`` runs after the last byte of a streamed turn rather than
    after its headers, and so a store outage cannot turn into a 500."""

    def __init__(self, app: Any, world: DurableWorld) -> None:
        self.app = app
        self.world = world

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.world.enabled:
            await self.app(scope, receive, send)
            return
        await self.world.load()
        try:
            await self.app(scope, receive, send)
        finally:
            await self.world.save()


# Every catalog field an applied merchant change moves: the price and stock flag, the
# listing copy, and the attribute chips (a promotion, a scarcity count, a fixed spec).
# Nothing else about a product is state — the rest is the fixture.
CATALOG_FIELDS = (
    "price",
    "in_stock",
    "title",
    "short_description",
    "long_description",
    "category",
    "attributes",
)


def catalog_state(state: WorldState, records: Mapping[str, Any], name: str = "catalog") -> None:
    """Register a catalog's moved fields. Pass every record a listing id resolves to —
    plain products, families, and variants — since a change can target any of them."""
    state.overlay(name, records, CATALOG_FIELDS)


def cart_lines_state(state: WorldState, carts: Any, name: str = "carts") -> None:
    """Register a ``SessionCarts``' lines. Kept here so the three mocks that use one
    declare it the same way."""
    from shopping_agent import CartItem

    state.nested_models(name, carts.lines_by_session(), CartItem)


def ledger_state(state: WorldState, ledger: Any, name: str = "ledger") -> None:
    """Register a ``ChangeLedger``: the staged, applied, and discarded changes, and the
    sequence the next change id comes from."""
    state.part(name, dump=ledger.snapshot, load=lambda document: ledger.restore(document or {}))


def dataclass_map_state(
    state: WorldState,
    name: str,
    target: MutableMapping[str, Any],
    dump: Callable[[Any], dict[str, Any]],
    load: Callable[[Mapping[str, Any]], Any],
) -> None:
    """A map of dataclasses, with the pair of functions that turn one into JSON and back
    (the ticketing engine's holds, offers, transfers, and tickets)."""

    def dump_all() -> Any:
        return {key: dump(value) for key, value in target.items()}

    def load_all(document: Any) -> None:
        target.clear()
        target.update({key: load(entry) for key, entry in (document or {}).items()})

    state.part(name, dump=dump_all, load=load_all)


def dataclass_lists_state(
    state: WorldState,
    name: str,
    target: MutableMapping[str, list[Any]],
    dump: Callable[[Any], dict[str, Any]],
    load: Callable[[Mapping[str, Any]], Any],
) -> None:
    """A map of ordered dataclass lists (a waitlist per tier)."""

    def dump_all() -> Any:
        return {key: [dump(item) for item in items] for key, items in target.items()}

    def load_all(document: Any) -> None:
        target.clear()
        for key, items in (document or {}).items():
            target[key] = [load(entry) for entry in items or []]

    state.part(name, dump=dump_all, load=load_all)
