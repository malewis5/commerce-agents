# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""The session record, its store, and the request dependency that loads and writes it back.

``start`` is the only place a principal enters the store: it binds a shopper profile (or,
on the merchant side, the merchant id) to a fresh unguessable session id. Every later
request carries that id in ``SESSION_HEADER`` and the routes read the principal from the
record, so no request shape names a user. A deployment authenticates the caller before
``start`` and passes the principal it verified; ``session_dependency`` and the routes stay.

A session is stored as two things, so a deployment's shared store lets any process find it; this in-memory store is per-process and a long session
stays cheap to write: a small state document (principal, provenance state, queued app
events), rewritten under a new version only when it changed, and the transcript, which a
request appends its new messages to. The dependency writes the record back when the
request ends; a streamed turn writes back when its stream ends (``stream_turn`` in
``host.py``); code holding a record outside a request calls ``save`` itself. A write whose
version is behind the store's is refused, so two requests racing on one session cannot
overwrite each other. ``SessionStore`` keeps both parts in memory; ``DurableSessionStore``
puts the same six storage methods on a ``StateStore``, which is what a deployment running
more than one process needs and what ``session_store`` returns when the environment names
a store.

Every method is async because a shared store is a network call: nothing on a request path
of a streaming API may block the loop.
"""

from __future__ import annotations

import copy
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

from .state import SESSION_TTL_S, StateConflict, StateStore, deployment_state_store

SESSION_HEADER = "X-Session-Id"

StateT = TypeVar("StateT", bound=BaseModel)


class UnknownSessionError(LookupError):
    """No live session has this id."""


class SessionConflictError(RuntimeError):
    """Another request wrote this session first; the caller retries from a fresh load."""


@dataclass
class SessionRecord(Generic[StateT]):
    session_id: str
    user_id: str
    state: StateT
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Actions taken outside the conversation (a button, a server-side event) since the
    # agent's last reply; the next chat turn hands them to the model as a note.
    pending_app_events: list[str] = field(default_factory=list)
    # What the store holds, so ``save`` writes only the difference: the state document's
    # version and content as loaded, and how many of ``messages`` are stored. A turn that
    # rewrote earlier messages sets ``stored_messages`` to 0 and the transcript is written whole.
    version: int = 0
    stored_state: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    stored_messages: int = field(default=0, repr=False, compare=False)
    ended: bool = field(default=False, repr=False, compare=False)

    def state_document(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "state": self.state.model_dump(mode="json"),
            "pending_app_events": list(self.pending_app_events),
        }


class SessionStore(Generic[StateT]):
    def __init__(self, state_type: type[StateT]) -> None:
        self._state_type = state_type
        self._states: dict[str, tuple[int, dict[str, Any]]] = {}
        self._transcripts: dict[str, list[dict[str, Any]]] = {}

    async def start(self, user_id: str) -> SessionRecord[StateT]:
        record: SessionRecord[StateT] = SessionRecord(
            session_id=secrets.token_urlsafe(24), user_id=user_id, state=self._state_type()
        )
        await self.save(record)
        return record

    async def require(self, session_id: str) -> SessionRecord[StateT]:
        stored = await self.read_state(session_id)
        if stored is None:
            raise UnknownSessionError(session_id)
        version, document = stored
        messages = await self.read_messages(session_id)
        return SessionRecord(
            session_id=session_id,
            user_id=document["user_id"],
            state=self._state_type.model_validate(document["state"]),
            messages=messages,
            pending_app_events=list(document["pending_app_events"]),
            version=version,
            stored_state=document,
            stored_messages=len(messages),
        )

    async def save(self, record: SessionRecord[StateT]) -> None:
        """The state document first, under the version check, whenever it changed or the
        transcript grew, so a request that lost a race writes nothing at all; then the
        messages the store lacks."""
        if record.ended:
            return
        document = record.state_document()
        grew = record.stored_messages < len(record.messages)
        if document != record.stored_state or grew:
            await self.write_state(record.session_id, document, record.version)
            record.version += 1
            record.stored_state = document
        if grew:
            new = record.messages[record.stored_messages :]
            await self.write_messages(record.session_id, new, record.stored_messages)
            record.stored_messages = len(record.messages)

    async def reset(self, record: SessionRecord[StateT]) -> None:
        record.ended = True
        await self.delete(record.session_id)

    async def sessions_for_user(self, user_id: str) -> list[SessionRecord[StateT]]:
        return [
            await self.require(session_id)
            for session_id in await self.session_ids_for_user(user_id)
        ]

    # -- Storage: the six methods a deployment puts over its own store.

    async def read_state(self, session_id: str) -> tuple[int, dict[str, Any]] | None:
        return self._states.get(session_id)

    async def write_state(self, session_id: str, document: dict[str, Any], version: int) -> None:
        """Store ``document`` as ``version + 1`` if the stored version is still ``version``
        (0 while a session is being started): a compare-and-set in a shared store."""
        current = self._states.get(session_id)
        if (current[0] if current else 0) != version:
            raise SessionConflictError(session_id)
        self._states[session_id] = (version + 1, document)

    # Copied both ways, as a real store would: a record's later edits reach the store only
    # through save.
    async def read_messages(self, session_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._transcripts.get(session_id, []))

    async def write_messages(
        self, session_id: str, messages: list[dict[str, Any]], start: int
    ) -> None:
        """Replace the transcript from ``start`` on: an append when ``start`` is its stored
        length, the whole transcript after a turn compacted it."""
        self._transcripts.setdefault(session_id, [])[start:] = copy.deepcopy(messages)

    async def delete(self, session_id: str) -> None:
        self._states.pop(session_id, None)
        self._transcripts.pop(session_id, None)

    async def session_ids_for_user(self, user_id: str) -> list[str]:
        return [
            session_id
            for session_id, (_, document) in self._states.items()
            if document["user_id"] == user_id
        ]


class DurableSessionStore(SessionStore[StateT]):
    """The six storage methods on a ``StateStore``: the state document is a versioned
    document, the transcript an appendable list, and each user's live session ids a member
    set (which ``sessions_for_user`` reads, for the server-side events a vertical delivers
    to a session that is not the caller's). Keys carry a role prefix, so the two roles of
    one deployment cannot resolve each other's session ids, and everything expires, so a
    demo left running does not keep transcripts forever."""

    def __init__(
        self,
        state_type: type[StateT],
        store: StateStore,
        *,
        prefix: str,
        ttl_s: int = SESSION_TTL_S,
    ) -> None:
        super().__init__(state_type)
        self._store = store
        self._prefix = prefix.rstrip(":")
        self._ttl_s = ttl_s

    def _document_key(self, session_id: str) -> str:
        return f"{self._prefix}:session:{session_id}"

    def _messages_key(self, session_id: str) -> str:
        return f"{self._prefix}:session:{session_id}:messages"

    def _user_key(self, user_id: str) -> str:
        return f"{self._prefix}:user:{user_id}:sessions"

    async def read_state(self, session_id: str) -> tuple[int, dict[str, Any]] | None:
        return await self._store.read_document(self._document_key(session_id))

    async def write_state(self, session_id: str, document: dict[str, Any], version: int) -> None:
        try:
            await self._store.write_document(
                self._document_key(session_id), document, version, ttl_s=self._ttl_s
            )
        except StateConflict as conflict:
            raise SessionConflictError(session_id) from conflict
        if version == 0:
            # The user index is written after the session exists, so it never names one
            # that a lost race did not create.
            await self._store.add_member(
                self._user_key(document["user_id"]), session_id, ttl_s=self._ttl_s
            )

    async def read_messages(self, session_id: str) -> list[dict[str, Any]]:
        return await self._store.read_list(self._messages_key(session_id))

    async def write_messages(
        self, session_id: str, messages: list[dict[str, Any]], start: int
    ) -> None:
        await self._store.write_list(
            self._messages_key(session_id), messages, start, ttl_s=self._ttl_s
        )

    async def delete(self, session_id: str) -> None:
        stored = await self._store.read_document(self._document_key(session_id))
        if stored is not None:
            await self._store.remove_member(self._user_key(stored[1]["user_id"]), session_id)
        await self._store.delete(self._document_key(session_id), self._messages_key(session_id))

    async def session_ids_for_user(self, user_id: str) -> list[str]:
        ids = await self._store.read_members(self._user_key(user_id))
        # An id whose session has expired stays in the set until something reads it.
        live = []
        for session_id in ids:
            if await self._store.read_document(self._document_key(session_id)) is not None:
                live.append(session_id)
            else:
                await self._store.remove_member(self._user_key(user_id), session_id)
        return live


def session_store(
    state_type: type[StateT], *, prefix: str, store: StateStore | None = None
) -> SessionStore[StateT]:
    """The store for one role: the deployment's shared one when the environment names it
    (``state.py``), the per-process one otherwise."""
    resolved = store if store is not None else deployment_state_store()
    if resolved is None:
        return SessionStore(state_type)
    return DurableSessionStore(state_type, resolved, prefix=prefix)


def session_dependency(store: SessionStore[StateT], start_route: str) -> Any:
    """The parameter annotation every scoped route of one role declares: the header's
    session id, resolved to its record and written back before the response goes out
    (FastAPI's function scope; a streamed turn writes back again when its stream ends).
    ``start_route`` names the login route in the 401 detail; a write that another request
    beat is a 409."""

    async def current_session(
        session_id: Annotated[str | None, Header(alias=SESSION_HEADER)] = None,
    ) -> AsyncIterator[SessionRecord[StateT]]:
        if not session_id:
            raise HTTPException(
                status_code=401, detail=f"Start a session first (POST {start_route})"
            )
        try:
            record = await store.require(session_id)
        except UnknownSessionError as error:
            raise HTTPException(status_code=401, detail="Unknown session") from error
        yield record
        try:
            await store.save(record)
        except SessionConflictError as error:
            raise HTTPException(status_code=409, detail="The session changed; retry") from error

    return Annotated[SessionRecord[StateT], Depends(current_session, scope="function")]
