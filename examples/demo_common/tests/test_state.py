# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""The state store contract, checked against both implementations.

``FakeRedis`` answers the commands ``RedisStateStore`` sends the way a Redis server would,
so the same suite runs over the in-process store and over the Redis dialect. The
compare-and-set script is the one place the store leans on the server, so it is
interpreted rather than stubbed: what is under test is that the keys, arguments, and
return values line up.
"""

import json

import pytest

from demo_common.state import (
    MemoryStateStore,
    RedisStateStore,
    StateConflict,
    deployment_state_store,
    reset_state_store,
)


class FakeRedis:
    """The handful of commands the store sends, over a dict."""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.expiries: dict[str, int] = {}
        self.sent: list[list[object]] = []

    async def command(self, *args):
        return self._run(list(args))

    async def pipeline(self, *commands):
        return [self._run(list(command)) for command in commands]

    def _run(self, command: list):
        self.sent.append(command)
        name = str(command[0]).upper()
        args = command[1:]
        if name == "GET":
            return self.data.get(args[0])
        if name == "SET":
            self.data[args[0]] = str(args[1])
            return "OK"
        if name == "DEL":
            removed = sum(1 for key in args if self.data.pop(key, None) is not None)
            return removed
        if name == "EXPIRE":
            self.expiries[args[0]] = int(args[1])
            return 1
        if name == "RPUSH":
            bucket = self.data.setdefault(args[0], [])
            bucket.extend(args[1:])
            return len(bucket)
        if name == "LRANGE":
            return list(self.data.get(args[0], []))
        if name == "LTRIM":
            bucket = self.data.get(args[0], [])
            start, stop = int(args[1]), int(args[2])
            self.data[args[0]] = bucket[start : None if stop == -1 else stop + 1]
            return "OK"
        if name == "HSET":
            bucket = self.data.setdefault(args[0], {})
            for field, value in zip(args[1::2], args[2::2], strict=True):
                bucket[field] = value
            return 1
        if name == "HGETALL":
            # The flat array a Redis server returns, which the store has to pair up.
            return [item for pair in self.data.get(args[0], {}).items() for item in pair]
        if name == "HDEL":
            return int(self.data.get(args[0], {}).pop(args[1], None) is not None)
        if name == "SADD":
            self.data.setdefault(args[0], set()).add(args[1])
            return 1
        if name == "SREM":
            self.data.get(args[0], set()).discard(args[1])
            return 1
        if name == "SMEMBERS":
            return list(self.data.get(args[0], set()))
        if name == "INCR":
            self.data[args[0]] = str(int(self.data.get(args[0], 0)) + 1)
            return int(self.data[args[0]])
        if name == "EVAL":
            return self._compare_and_set(args)
        raise AssertionError(f"unexpected command {name}")

    def _compare_and_set(self, args):
        _script, _keys, version_key, document_key, version, document, ttl = args
        stored = int(self.data.get(version_key, 0))
        if stored != int(version):
            return stored
        self.data[version_key] = str(stored + 1)
        self.data[document_key] = document
        if int(ttl) > 0:
            self.expiries[version_key] = self.expiries[document_key] = int(ttl)
        return -1


@pytest.fixture(params=["in-process", "redis"])
def store(request):
    if request.param == "in-process":
        return MemoryStateStore()
    return RedisStateStore(FakeRedis(), namespace="acme")


async def test_a_document_reads_back_with_its_version_and_refuses_a_stale_write(store):
    assert await store.read_document("world") is None
    assert await store.write_document("world", {"carts": {}}, 0) == 1
    assert await store.read_document("world") == (1, {"carts": {}})
    assert await store.write_document("world", {"carts": {"s-1": 2}}, 1) == 2
    with pytest.raises(StateConflict):
        await store.write_document("world", {"carts": {}}, 1)
    assert await store.read_document("world") == (2, {"carts": {"s-1": 2}})


async def test_a_list_appends_from_its_length_and_truncates_from_anywhere(store):
    assert await store.read_list("messages") == []
    await store.write_list("messages", [{"n": 1}, {"n": 2}], 0)
    await store.write_list("messages", [{"n": 3}], 2)
    assert await store.read_list("messages") == [{"n": 1}, {"n": 2}, {"n": 3}]
    await store.write_list("messages", [{"n": 9}], 1)  # a compacted transcript
    assert await store.read_list("messages") == [{"n": 1}, {"n": 9}]
    await store.write_list("messages", [], 0)
    assert await store.read_list("messages") == []


async def test_a_map_is_written_and_deleted_by_field(store):
    await store.write_map("facts", {"size": {"value": "medium"}})
    await store.write_map("facts", {"budget": {"value": "under $60"}})
    assert await store.read_map("facts") == {
        "size": {"value": "medium"},
        "budget": {"value": "under $60"},
    }
    assert await store.delete_map_field("facts", "size") is True
    assert await store.delete_map_field("facts", "size") is False
    assert await store.read_map("facts") == {"budget": {"value": "under $60"}}


async def test_members_and_counters(store):
    await store.add_member("sessions", "s-1")
    await store.add_member("sessions", "s-2")
    assert await store.read_members("sessions") == ["s-1", "s-2"]
    await store.remove_member("sessions", "s-1")
    assert await store.read_members("sessions") == ["s-2"]
    assert await store.read_counter("purges") == 0
    assert await store.increment("purges") == 1
    assert await store.read_counter("purges") == 1


async def test_delete_forgets_a_document_and_its_version(store):
    await store.write_document("world", {"a": 1}, 0)
    await store.delete("world")
    assert await store.read_document("world") is None
    assert await store.write_document("world", {"a": 2}, 0) == 1


async def test_the_redis_store_namespaces_its_keys_and_sets_the_expiry_it_is_given():
    redis = FakeRedis()
    store = RedisStateStore(redis, namespace="acme")
    await store.write_document("shopper:session:s-1", {"user_id": "demo"}, 0, ttl_s=60)
    assert json.loads(redis.data["acme:shopper:session:s-1"]) == {"user_id": "demo"}
    assert redis.expiries["acme:shopper:session:s-1"] == 60
    await store.write_list("shopper:session:s-1:messages", [{"n": 1}], 0, ttl_s=60)
    assert redis.expiries["acme:shopper:session:s-1:messages"] == 60


async def test_the_environment_decides_which_store_a_deployment_gets(monkeypatch):
    for variable in (
        "COMMERCE_STATE_STORE",
        "KV_REST_API_URL",
        "KV_REST_API_TOKEN",
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
    ):
        monkeypatch.delenv(variable, raising=False)

    reset_state_store()
    assert deployment_state_store() is None  # a local run keeps its state in the process

    reset_state_store()
    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "token")
    assert isinstance(deployment_state_store(), RedisStateStore)

    reset_state_store()
    monkeypatch.setenv("COMMERCE_STATE_STORE", "none")
    assert deployment_state_store() is None

    reset_state_store()
    monkeypatch.setenv("COMMERCE_STATE_STORE", "memory")
    assert isinstance(deployment_state_store(), MemoryStateStore)
    reset_state_store()
