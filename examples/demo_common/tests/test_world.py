# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""The world document: what travels, what is reset, and when it is written.

Two ``DurableWorld`` objects over one ``MemoryStateStore`` are two processes of one
deployment, which is the case the document exists for.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from demo_common import MemoryStateStore
from demo_common.world import DurableWorld, WorldMiddleware, WorldState, declared_once


class Line(BaseModel):
    product_id: str
    quantity: int = 1


class Record(BaseModel):
    """Stands in for a catalog record: a couple of fields a change can move."""

    price: float
    attributes: dict[str, str] = {}


class Mock:
    """A mock backend with one of each shape: a plain container, a map of records, a map
    of maps of them, and a catalog whose moved fields travel as a difference."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, int]] = {"AR-1": {"stock": 4}}
        self.lines: dict[str, dict[str, Line]] = {}
        self.drafts: dict[str, Line] = {}
        self.catalog = {"AR-1": Record(price=39.0), "AR-2": Record(price=12.0)}

    @declared_once
    def world_state(self) -> WorldState:
        state = WorldState()
        state.container("rows", self.rows)
        state.nested_models("lines", self.lines, Line)
        state.models("drafts", self.drafts, Line)
        state.overlay("catalog", self.catalog, ("price", "attributes"))
        return state


def through_json(document: dict) -> dict:
    """What the store does to a snapshot on the way out and back."""
    return json.loads(json.dumps(document))


def test_every_shape_round_trips_and_a_dropped_key_is_reset():
    edited, fresh = Mock(), Mock()
    edited.world_state()  # declared where the fixtures land, as a host builds it
    edited.rows["AR-1"]["stock"] = 9
    edited.rows["AR-9"] = {"stock": 1}
    edited.lines["s-1"] = {"AR-1": Line(product_id="AR-1", quantity=2)}
    edited.drafts["chg-1"] = Line(product_id="AR-2")
    document = through_json(edited.world_state().snapshot())

    state = fresh.world_state()
    fresh.rows["AR-8"] = {"stock": 7}  # this process's own, and not in the document
    state.restore(document)
    assert fresh.rows == {"AR-1": {"stock": 9}, "AR-9": {"stock": 1}}
    assert fresh.lines["s-1"]["AR-1"] == Line(product_id="AR-1", quantity=2)
    assert fresh.drafts == {"chg-1": Line(product_id="AR-2")}


def test_a_catalog_carries_only_what_moved_and_the_rest_goes_back_to_the_fixtures():
    edited, fresh = Mock(), Mock()
    edited.world_state()
    edited.catalog["AR-1"].price = 34.0
    edited.catalog["AR-1"].attributes["promotion"] = "Weekend 10% off"
    document = through_json(edited.world_state().snapshot())
    assert set(document["catalog"]) == {"AR-1"}  # AR-2 never moved, so it does not travel

    state = fresh.world_state()
    fresh.catalog["AR-2"].price = 99.0  # a stale edit this process made on its own
    state.restore(document)
    assert fresh.catalog["AR-1"].price == 34.0
    assert fresh.catalog["AR-1"].attributes == {"promotion": "Weekend 10% off"}
    assert fresh.catalog["AR-2"].price == 12.0  # back to its fixture value


def test_a_record_two_ids_resolve_to_is_written_rather_than_replaced():
    """A family and its variants are the same objects under two keys, so a restore that
    replaced records would break the sharing the catalog relies on."""
    shared = Record(price=10.0)
    mock, peer = Mock(), Mock()
    mock.catalog = {"AR-1": shared, "AR-1-KING": shared}
    peer_shared = Record(price=10.0)
    peer.catalog = {"AR-1": peer_shared, "AR-1-KING": peer_shared}
    mock.world_state()
    peer.world_state()
    shared.price = 15.0
    peer.world_state().restore(through_json(mock.world_state().snapshot()))
    assert peer.catalog["AR-1"] is peer.catalog["AR-1-KING"]
    assert peer_shared.price == 15.0


def test_parts_are_named_once_and_folded_in_under_a_prefix():
    state = WorldState()
    state.include(Mock().world_state(), prefix="storefront")
    state.include(Mock().world_state(), prefix="merchant")
    assert "storefront.rows" in state.names and "merchant.rows" in state.names
    with pytest.raises(ValueError, match="already has a part"):
        state.include(Mock().world_state(), prefix="merchant")


async def test_with_no_store_the_world_does_nothing():
    mock = Mock()
    world = DurableWorld(None, "retail:world")
    world.include(mock, prefix="storefront")
    assert world.enabled is False
    await world.load()
    mock.rows["AR-1"]["stock"] = 1
    await world.save()  # no store to write to, and no error either


async def test_one_process_writes_what_the_next_one_reads():
    store = MemoryStateStore()
    first, second = Mock(), Mock()
    writer = DurableWorld(store, "retail:world")
    writer.include(first, prefix="storefront")
    reader = DurableWorld(store, "retail:world")
    reader.include(second, prefix="storefront")

    await writer.load()
    first.lines["s-1"] = {"AR-1": Line(product_id="AR-1", quantity=3)}
    first.catalog["AR-1"].price = 30.0
    await writer.save()

    await reader.load()
    assert second.lines["s-1"]["AR-1"].quantity == 3
    assert second.catalog["AR-1"].price == 30.0
    await reader.save()
    assert (await store.read_document("retail:world"))[0] == 1  # a read wrote nothing


async def test_a_write_that_lost_the_race_goes_in_anyway_and_says_so(caplog):
    store = MemoryStateStore()
    mine, theirs = Mock(), Mock()
    ours = DurableWorld(store, "retail:world")
    ours.include(mine, prefix="storefront")
    await ours.load()
    mine.rows["AR-1"]["stock"] = 1

    other = DurableWorld(store, "retail:world")
    other.include(theirs, prefix="storefront")
    await other.load()
    theirs.rows["AR-1"]["stock"] = 2
    await other.save()

    await ours.save()
    assert "wrote first" in caplog.text
    assert (await store.read_document("retail:world"))[1]["storefront.rows"]["AR-1"]["stock"] == 1


async def test_the_middleware_writes_after_a_streamed_response_has_finished():
    store = MemoryStateStore()
    mock = Mock()
    world = DurableWorld(store, "retail:world")
    world.include(mock, prefix="storefront")
    app = FastAPI()

    @app.post("/chat")
    async def chat() -> StreamingResponse:
        def turn():
            yield b"data: thinking\n\n"
            mock.rows["AR-1"]["stock"] = 0  # the tool call, mid-stream
            yield b"data: done\n\n"

        return StreamingResponse(turn(), media_type="text/event-stream")

    @app.get("/cart")
    async def cart() -> dict:
        return {"stock": mock.rows["AR-1"]["stock"]}

    app.add_middleware(WorldMiddleware, world=world)
    client = TestClient(app)
    assert client.get("/cart").json() == {"stock": 4}
    assert await store.read_document("retail:world") is None  # a read writes nothing

    client.post("/chat")
    stored = await store.read_document("retail:world")
    assert stored is not None and stored[1]["storefront.rows"]["AR-1"]["stock"] == 0


def test_world_state_is_declared_once_so_the_fixture_values_stay_the_fixtures():
    mock = Mock()
    assert mock.world_state() is mock.world_state()
