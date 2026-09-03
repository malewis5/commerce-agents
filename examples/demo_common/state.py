# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""The shared store the deployed examples keep their state in.

Locally a demo is one process, so sessions, carts, and staged changes can live in its
memory. A deployment is not one process: a platform runs as many as traffic asks for and
replaces them freely, and the filesystem a function sees is read-only and gone with the
instance. Everything a demo mutates therefore moves behind ``StateStore``, whose four
shapes are what the examples' state actually is:

    a versioned document   a session's state, the mock world             read/write with a
                           of one deployment                             compare-and-set
    an appendable list     a session transcript                          append or truncate
    a field map            what is remembered about one subject          per-field writes
    a member set           which sessions belong to one user             add/remove

``RedisStateStore`` puts them on Redis over HTTP, the dialect Upstash serves and the
Vercel Marketplace provisions; it speaks HTTP rather than the wire protocol because a
function that handles one request has nowhere to keep a connection pool.
``MemoryStateStore`` is the same four shapes in a dict, for the tests and for a run that
wants the deployed code paths without a store. A deployment with its own store implements
the protocol over that instead; nothing above this module knows which one it has.

``deployment_state_store()`` returns the store the environment describes, or ``None`` when
it describes none — which is what a local `run_demo.py` gets, and why the examples still
run with no store at all.

| Variable | Effect |
|---|---|
| ``KV_REST_API_URL``, ``KV_REST_API_TOKEN`` | The Redis endpoint, as the Vercel Marketplace names it |
| ``UPSTASH_REDIS_REST_URL``, ``UPSTASH_REDIS_REST_TOKEN`` | The same endpoint, as Upstash names it (read second) |
| ``COMMERCE_STATE_NAMESPACE`` | Key prefix, so several deployments can share one store |
| ``COMMERCE_STATE_STORE`` | ``memory`` forces the in-process store; ``none`` forces no store |
"""

from __future__ import annotations

import copy
import json
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Sessions expire on their own so a demo that ran for a week is not still paying for the
# transcripts; the world document has no expiry, since it is the deployment's fixtures.
SESSION_TTL_S = 7 * 24 * 60 * 60


class StateConflict(RuntimeError):
    """A versioned write lost a race; the caller reloads and retries, or gives way."""


@runtime_checkable
class StateStore(Protocol):
    """The four shapes above. Every method is async: a shared store is a network call,
    and a demo API streams, so nothing here may block the loop."""

    # -- A versioned document: read returns ``(version, document)``, and a write is
    #    refused unless the stored version is still the one that was read.

    async def read_document(self, key: str) -> tuple[int, dict[str, Any]] | None: ...

    async def write_document(
        self, key: str, document: Mapping[str, Any], version: int, *, ttl_s: int | None = None
    ) -> int:
        """Store ``document`` as ``version + 1`` when the stored version is still
        ``version`` (0 for a key that does not exist yet), and return the new version.
        Raise ``StateConflict`` otherwise."""

    # -- An appendable list of JSON objects.

    async def read_list(self, key: str) -> list[dict[str, Any]]: ...

    async def write_list(
        self,
        key: str,
        items: Sequence[Mapping[str, Any]],
        start: int,
        *,
        ttl_s: int | None = None,
    ) -> None:
        """Replace the list from ``start`` on: an append when ``start`` is its length."""

    # -- A field map, written and deleted one field at a time.

    async def read_map(self, key: str) -> dict[str, Any]: ...

    async def write_map(
        self, key: str, entries: Mapping[str, Any], *, ttl_s: int | None = None
    ) -> None: ...

    async def delete_map_field(self, key: str, field: str) -> bool: ...

    # -- A member set.

    async def read_members(self, key: str) -> list[str]: ...

    async def add_member(self, key: str, member: str, *, ttl_s: int | None = None) -> None: ...

    async def remove_member(self, key: str, member: str) -> None: ...

    # -- Whole keys, and a counter (the memory purge generation).

    async def read_counter(self, key: str) -> int: ...

    async def increment(self, key: str) -> int: ...

    async def delete(self, *keys: str) -> None: ...


# ---------------------------------------------------------------------------
# In-process
# ---------------------------------------------------------------------------


class MemoryStateStore:
    """The four shapes in one process's memory. Values are copied both ways, as a real
    store would, so a caller's later edits reach the store only through a write."""

    def __init__(self) -> None:
        self._documents: dict[str, tuple[int, dict[str, Any]]] = {}
        self._lists: dict[str, list[dict[str, Any]]] = {}
        self._maps: dict[str, dict[str, Any]] = {}
        self._sets: dict[str, set[str]] = {}
        self._counters: dict[str, int] = {}

    async def read_document(self, key: str) -> tuple[int, dict[str, Any]] | None:
        stored = self._documents.get(key)
        return (stored[0], copy.deepcopy(stored[1])) if stored else None

    async def write_document(
        self, key: str, document: Mapping[str, Any], version: int, *, ttl_s: int | None = None
    ) -> int:
        del ttl_s  # nothing in this process outlives it
        current = self._documents.get(key)
        if (current[0] if current else 0) != version:
            raise StateConflict(key)
        self._documents[key] = (version + 1, copy.deepcopy(dict(document)))
        return version + 1

    async def read_list(self, key: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._lists.get(key, []))

    async def write_list(
        self,
        key: str,
        items: Sequence[Mapping[str, Any]],
        start: int,
        *,
        ttl_s: int | None = None,
    ) -> None:
        del ttl_s
        self._lists.setdefault(key, [])[start:] = copy.deepcopy([dict(item) for item in items])

    async def read_map(self, key: str) -> dict[str, Any]:
        return copy.deepcopy(self._maps.get(key, {}))

    async def write_map(
        self, key: str, entries: Mapping[str, Any], *, ttl_s: int | None = None
    ) -> None:
        del ttl_s
        self._maps.setdefault(key, {}).update(copy.deepcopy(dict(entries)))

    async def delete_map_field(self, key: str, field: str) -> bool:
        return self._maps.get(key, {}).pop(field, None) is not None

    async def read_members(self, key: str) -> list[str]:
        return sorted(self._sets.get(key, set()))

    async def add_member(self, key: str, member: str, *, ttl_s: int | None = None) -> None:
        del ttl_s
        self._sets.setdefault(key, set()).add(member)

    async def remove_member(self, key: str, member: str) -> None:
        self._sets.get(key, set()).discard(member)

    async def read_counter(self, key: str) -> int:
        return self._counters.get(key, 0)

    async def increment(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._documents.pop(key, None)
            self._lists.pop(key, None)
            self._maps.pop(key, None)
            self._sets.pop(key, None)
            self._counters.pop(key, None)


# ---------------------------------------------------------------------------
# Redis over HTTP
# ---------------------------------------------------------------------------

# The compare-and-set behind ``write_document``: the version lives in its own key so the
# check is one integer read, and both keys move together or not at all.
_WRITE_DOCUMENT = """
local stored = tonumber(redis.call('GET', KEYS[1]) or '0')
if stored ~= tonumber(ARGV[1]) then return stored end
local next_version = stored + 1
redis.call('SET', KEYS[1], next_version)
redis.call('SET', KEYS[2], ARGV[2])
local ttl = tonumber(ARGV[3])
if ttl > 0 then
  redis.call('EXPIRE', KEYS[1], ttl)
  redis.call('EXPIRE', KEYS[2], ttl)
end
return -1
"""


class RestRedis:
    """Redis over HTTP: one POST carries one command, ``/pipeline`` carries several. The
    body is the command as a JSON array (``["GET", "key"]``) and the reply is
    ``{"result": ...}``, which is what Upstash's REST API and its work-alikes serve."""

    def __init__(self, url: str, token: str, *, timeout_s: float = 10.0) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._client: Any | None = None

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self._url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout_s,
            )
        return self._client

    async def command(self, *args: Any) -> Any:
        response = await self._http().post("/", json=[_arg(arg) for arg in args])
        response.raise_for_status()
        return _result(response.json(), args[0])

    async def pipeline(self, *commands: Sequence[Any]) -> list[Any]:
        if not commands:
            return []
        body = [[_arg(arg) for arg in command] for command in commands]
        response = await self._http().post("/pipeline", json=body)
        response.raise_for_status()
        replies = response.json()
        return [
            _result(reply, command[0]) for reply, command in zip(replies, commands, strict=True)
        ]


def _arg(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) else json.dumps(value)


def _result(reply: Any, command: Any) -> Any:
    if isinstance(reply, dict) and reply.get("error"):
        raise RuntimeError(f"{command} failed: {reply['error']}")
    return reply.get("result") if isinstance(reply, dict) else reply


class RedisStateStore:
    """``StateStore`` over ``RestRedis``. Keys are ``{namespace}:{key}``, so one store
    serves several deployments; documents are JSON strings, lists are Redis lists of
    them, maps are hashes, and sets are sets."""

    def __init__(self, redis: RestRedis, *, namespace: str = "commerce") -> None:
        self._redis = redis
        self._namespace = namespace.rstrip(":")

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def read_document(self, key: str) -> tuple[int, dict[str, Any]] | None:
        version, document = await self._redis.pipeline(
            ["GET", f"{self._key(key)}:version"], ["GET", self._key(key)]
        )
        if document is None:
            return None
        return int(version or 0), json.loads(document)

    async def write_document(
        self, key: str, document: Mapping[str, Any], version: int, *, ttl_s: int | None = None
    ) -> int:
        outcome = await self._redis.command(
            "EVAL",
            _WRITE_DOCUMENT,
            2,
            f"{self._key(key)}:version",
            self._key(key),
            version,
            json.dumps(document, separators=(",", ":")),
            ttl_s or 0,
        )
        if int(outcome) != -1:
            raise StateConflict(f"{key}: stored version is {int(outcome)}, not {version}")
        return version + 1

    async def read_list(self, key: str) -> list[dict[str, Any]]:
        items = await self._redis.command("LRANGE", self._key(key), 0, -1)
        return [json.loads(item) for item in items or []]

    async def write_list(
        self,
        key: str,
        items: Sequence[Mapping[str, Any]],
        start: int,
        *,
        ttl_s: int | None = None,
    ) -> None:
        full = self._key(key)
        # LTRIM keeps 0..start-1; at start 0 there is nothing to keep, and LTRIM 0 -1
        # would keep everything, so the key goes instead.
        commands: list[Sequence[Any]] = [
            ["DEL", full] if start == 0 else ["LTRIM", full, 0, start - 1]
        ]
        if items:
            commands.append(
                ["RPUSH", full, *(json.dumps(item, separators=(",", ":")) for item in items)]
            )
        if ttl_s:
            commands.append(["EXPIRE", full, ttl_s])
        await self._redis.pipeline(*commands)

    async def read_map(self, key: str) -> dict[str, Any]:
        stored = await self._redis.command("HGETALL", self._key(key))
        return {field: json.loads(value) for field, value in _pairs(stored).items()}

    async def write_map(
        self, key: str, entries: Mapping[str, Any], *, ttl_s: int | None = None
    ) -> None:
        if not entries:
            return
        flat: list[Any] = []
        for field, value in entries.items():
            flat += [field, json.dumps(value, separators=(",", ":"))]
        commands: list[Sequence[Any]] = [["HSET", self._key(key), *flat]]
        if ttl_s:
            commands.append(["EXPIRE", self._key(key), ttl_s])
        await self._redis.pipeline(*commands)

    async def delete_map_field(self, key: str, field: str) -> bool:
        return int(await self._redis.command("HDEL", self._key(key), field) or 0) > 0

    async def read_members(self, key: str) -> list[str]:
        return sorted(await self._redis.command("SMEMBERS", self._key(key)) or [])

    async def add_member(self, key: str, member: str, *, ttl_s: int | None = None) -> None:
        commands: list[Sequence[Any]] = [["SADD", self._key(key), member]]
        if ttl_s:
            commands.append(["EXPIRE", self._key(key), ttl_s])
        await self._redis.pipeline(*commands)

    async def remove_member(self, key: str, member: str) -> None:
        await self._redis.command("SREM", self._key(key), member)

    async def read_counter(self, key: str) -> int:
        return int(await self._redis.command("GET", self._key(key)) or 0)

    async def increment(self, key: str) -> int:
        return int(await self._redis.command("INCR", self._key(key)))

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._redis.command(
                "DEL", *(self._key(key) for key in keys), *(f"{self._key(k)}:version" for k in keys)
            )


def _pairs(stored: Any) -> dict[str, str]:
    """A hash as the REST API returns it: a mapping, or the flat array Redis speaks."""
    if isinstance(stored, dict):
        return stored
    values = list(stored or [])
    return dict(zip(values[::2], values[1::2], strict=False))


# ---------------------------------------------------------------------------
# What the environment describes
# ---------------------------------------------------------------------------

# The Marketplace name first: a Vercel project with a Redis store connected has the KV_
# pair, and a store provisioned through Upstash directly has the UPSTASH_ pair.
_ENDPOINT_VARIABLES = (
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
)

_store: StateStore | None = None
_resolved = False


def state_namespace(default: str = "commerce") -> str:
    return os.environ.get("COMMERCE_STATE_NAMESPACE") or default


def deployment_state_store(namespace: str = "commerce") -> StateStore | None:
    """The store this process shares with the rest of its deployment, or ``None`` when the
    environment names none — a local run, which keeps its state in memory as before. The
    answer is computed once: every role of one process gets the same store."""
    global _store, _resolved
    if _resolved:
        return _store
    _resolved = True
    _store = _build_state_store(namespace)
    return _store


def reset_state_store() -> None:
    """Forget the resolved store, so a test can change the environment and ask again."""
    global _store, _resolved
    _store, _resolved = None, False


def _build_state_store(namespace: str) -> StateStore | None:
    choice = os.environ.get("COMMERCE_STATE_STORE", "").strip().lower()
    if choice == "none":
        return None
    if choice == "memory":
        logger.info("state: in-process store (COMMERCE_STATE_STORE=memory)")
        return MemoryStateStore()
    for url_variable, token_variable in _ENDPOINT_VARIABLES:
        url, token = os.environ.get(url_variable), os.environ.get(token_variable)
        if url and token:
            logger.info("state: Redis over HTTP from %s", url_variable)
            return RedisStateStore(RestRedis(url, token), namespace=state_namespace(namespace))
    return None
