# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

import json

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask

from demo_common import (
    SESSION_HEADER,
    DurableSessionStore,
    MemoryStateStore,
    SessionConflictError,
    SessionStore,
    UnknownSessionError,
    session_dependency,
)
from merchant_agent import AnalysisResult, Listing, MerchantSessionState
from shopping_agent import Product, ShoppingSessionState


@pytest.fixture(params=["in-process", "shared"])
def make_store(request):
    """Both stores the examples ship, so every rule below is checked against each: the
    per-process one a local demo runs on, and the one a deployment puts on a shared
    store (here the in-process ``StateStore``, which is the same code path a deployment's
    Redis takes)."""

    def build(state_type):
        if request.param == "in-process":
            return SessionStore(state_type)
        return DurableSessionStore(state_type, MemoryStateStore(), prefix="test")

    return build


async def test_start_binds_the_principal_and_mints_a_distinct_token_each_time(make_store):
    store = make_store(ShoppingSessionState)
    first, second = await store.start("demo-user"), await store.start("demo-user")
    other = await store.start("demo-user-2")
    assert first.session_id != second.session_id and len(first.session_id) >= 24
    assert await store.require(first.session_id) == first
    assert set(await store.session_ids_for_user("demo-user")) == {
        first.session_id,
        second.session_id,
    }
    assert [record.session_id for record in await store.sessions_for_user("demo-user-2")] == [
        other.session_id
    ]
    await store.reset(first)
    with pytest.raises(UnknownSessionError):
        await store.require(first.session_id)
    await store.save(first)  # a reset record writes nothing back
    assert await store.session_ids_for_user("demo-user") == [second.session_id]


async def test_save_moves_the_version_when_state_or_transcript_changed_and_appends_the_new_messages(
    make_store,
):
    store = make_store(ShoppingSessionState)
    record = await store.start("demo-user")
    record.messages.append({"role": "user", "content": "a kettle"})
    await store.save(record)
    assert record.version == 2  # the transcript grew, so the version moved with it
    await store.save(record)
    assert record.version == 2  # nothing new: no write
    record.state.remember_products([Product(product_id="P-1", title="Kettle", price=39.0)])
    record.messages.append({"role": "assistant", "content": "Here is one."})
    record.pending_app_events.append("Customer tapped add on Kettle (P-1).")
    await store.save(record)
    assert record.version == 3
    json.dumps(await store.read_state(record.session_id))
    loaded = await store.require(record.session_id)
    assert loaded == record and len(loaded.messages) == 2
    loaded.messages.append({"role": "user", "content": "add it"})
    assert len((await store.require(record.session_id)).messages) == 2  # until saved


async def test_a_turn_that_rewrote_earlier_messages_replaces_the_transcript(make_store):
    store = make_store(ShoppingSessionState)
    record = await store.start("demo-user")
    record.messages += [
        {"role": "user", "content": "x" * 50},
        {"role": "assistant", "content": "y"},
    ]
    await store.save(record)
    record.messages[0]["content"] = "[cleared]"
    record.messages.append({"role": "user", "content": "next"})
    await store.save(record)
    stored = await store.require(record.session_id)
    assert stored.messages[0]["content"] == "x" * 50  # appended only
    record.stored_messages = 0
    await store.save(record)
    contents = [m["content"] for m in (await store.require(record.session_id)).messages]
    assert contents == ["[cleared]", "y", "next"]


async def test_the_second_writer_of_one_version_is_refused_and_writes_nothing(make_store):
    store = make_store(MerchantSessionState)
    started = await store.start("acme")
    button = await store.require(started.session_id)
    turn = await store.require(started.session_id)
    button.pending_app_events.append("Operator approved change chg-1 from the preview card.")
    await store.save(button)
    turn.state.remember_listing_record(Listing(listing_id="L-1", title="Kettle", price=39.0))
    turn.messages.append({"role": "user", "content": "raise it"})
    with pytest.raises(SessionConflictError):
        await store.save(turn)
    stored = await store.require(started.session_id)
    assert stored.state.read_listings == set() and stored.messages == []


async def test_merchant_state_round_trips_with_its_sets_and_counters(make_store):
    store = make_store(MerchantSessionState)
    record = await store.start("acme")
    record.state.remember_listing_record(Listing(listing_id="L-1", title="Kettle", price=39.0))
    record.state.remember_analysis(AnalysisResult(question="why flat?", headline="Flat week."))
    await store.save(record)
    loaded = await store.require(record.session_id)
    assert loaded.state.read_listings == {"L-1"} and loaded.state.analyses_run == 1
    assert (
        loaded.state.remember_analysis(AnalysisResult(question="and now?", headline="Up."))
        == "AN-2"
    )


async def test_dependency_loads_writes_back_before_responding_and_reports_a_lost_race(make_store):
    store = make_store(ShoppingSessionState)
    current_session = session_dependency(store, "/api/session")
    app = FastAPI()

    @app.post("/note")
    async def note(record: current_session) -> dict:
        record.pending_app_events.append("tapped")
        return {"user": record.user_id}

    @app.post("/chat")
    async def chat(record: current_session) -> StreamingResponse:
        def turn():
            record.messages.append({"role": "assistant", "content": "streamed"})
            yield b"data: ...\n\n"

        return StreamingResponse(turn(), background=BackgroundTask(store.save, record))

    @app.post("/racing-note")
    async def racing_note(record: current_session) -> dict:
        rival = await store.require(record.session_id)
        rival.pending_app_events.append("first")
        await store.save(rival)
        record.pending_app_events.append("second")
        return {}

    client = TestClient(app)
    missing = client.post("/note")
    assert missing.status_code == 401 and "/api/session" in missing.json()["detail"]
    assert client.post("/note", headers={SESSION_HEADER: "made-up"}).status_code == 401
    headers = {SESSION_HEADER: (await store.start("demo-user-2")).session_id}
    assert client.post("/note", headers=headers).json() == {"user": "demo-user-2"}
    assert (await store.require(headers[SESSION_HEADER])).pending_app_events == ["tapped"]
    assert client.post("/chat", headers=headers).status_code == 200
    assert (await store.require(headers[SESSION_HEADER])).messages == [
        {"role": "assistant", "content": "streamed"}
    ]
    assert client.post("/racing-note", headers=headers).status_code == 409
    assert (await store.require(headers[SESSION_HEADER])).pending_app_events == [
        "tapped",
        "first",
    ]
