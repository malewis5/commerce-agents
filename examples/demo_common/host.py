# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Process-level plumbing both roles of a demo API share: credential loading, the app
with its host and CORS middleware, background tasks, the loopback guard, and the SSE
response one chat turn streams.

Two of those change shape when the API is deployed rather than run on a laptop. The
loopback guard is a local protection — it stops a page on another origin from reaching a
demo bound to 127.0.0.1 — so a deployment names the hosts it answers to instead
(``DEMO_ALLOWED_HOSTS``, or ``*`` where the platform routes by host itself). And a
serverless function stops running when its response ends, so the memory extraction a
local run leaves to a background task is awaited inside the stream instead: it is the last
thing a turn does either way, and dropping it would mean nothing was ever remembered."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from commerce_common.streaming import AgentEvent, to_sse
from commerce_common.turn import session_tag
from shopping_agent import Cart, Order, ProductDetails, ShoppingSessionContext

from .sessions import SessionConflictError, SessionRecord, SessionStore

logger = logging.getLogger(__name__)

# The repository, which is where the skills and the .env files are. A deployment that
# carries only the slice of the repository its API needs points this at that slice
# (``COMMERCE_REPO_ROOT``); everything below then resolves inside the bundle.
REPO_ROOT = Path(os.environ.get("COMMERCE_REPO_ROOT") or Path(__file__).resolve().parents[2])


class DemoStorefront(Protocol):
    """What the shared routes use from a vertical's mock storefront on top of the
    ``StorefrontBackend`` interface it implements: the listings (``products``), a lookup
    by id that also resolves a variant, the store name, per-session cleanup, and the
    cross-user order feed the merchant overview shows."""

    store_name: str
    products: dict[str, ProductDetails]

    def product(self, product_id: str) -> ProductDetails | None: ...

    def reset_session(self, session_id: str) -> None: ...

    def recent_orders(self, limit: int = 6) -> list[Order]: ...

    async def get_cart(self, session: ShoppingSessionContext) -> Cart: ...

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart: ...


def load_demo_env(example_root: Path) -> None:
    """Load credentials before any agent is constructed. A variable already in the
    environment wins; the example's own ``.env`` fills in the rest, then the repo-root
    one; ``COMMERCE_DEMO_AUTH=sdk`` clears key variables instead so the Anthropic SDK's
    own credential chain is used."""
    if os.environ.get("COMMERCE_DEMO_AUTH", "").lower() == "sdk":
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    else:
        load_dotenv(example_root / ".env", override=False)
        load_dotenv(REPO_ROOT / ".env", override=False)


def host_approval_default() -> bool:
    """Merchant portals require host approval unless ``MERCHANT_REQUIRE_HOST_APPROVAL=0``."""
    return os.environ.get("MERCHANT_REQUIRE_HOST_APPROVAL", "1") != "0"


# The event loop holds only weak references to tasks, so fire-and-forget work (memory
# extraction after a turn) is kept alive here until it completes.
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn_background(coro: Coroutine[Any, Any, object]) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def hosted() -> bool:
    """Whether this process is a deployment's rather than a laptop's. Three things below
    read it — the hosts the API answers to, whether work after a response runs, and where
    the credential messages tell a reader to put the key — and each has its own variable
    to say so outright where this guess is wrong."""
    return bool(os.environ.get("VERCEL"))


def background_outlives_response() -> bool:
    """Whether work started after a response is sent will actually run. A process that
    stays up (a local ``uvicorn``, a container) finishes it; a function the platform
    freezes when the response ends does not, so its callers await instead. Set
    ``COMMERCE_INLINE_BACKGROUND`` to ``1`` or ``0`` to say so directly."""
    stated = os.environ.get("COMMERCE_INLINE_BACKGROUND", "").strip()
    if stated:
        return stated == "0"
    return not hosted()


def credentials_hint(example_dir: str) -> str:
    """Where a reader of a credential error should put the key. On a laptop that is a
    file; on a deployment there is no file to edit, only the project's environment."""
    if hosted():
        return "this deployment's environment variables"
    return f"examples/{example_dir}/.env or the repo-root .env"


def _lifespan(on_startup: Sequence[Callable[[], Awaitable[None]]]):
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            logger.info(
                "No API key in the environment or .env files; the Anthropic SDK falls back "
                "to its own credential chain. If chat returns auth errors, set "
                "ANTHROPIC_API_KEY in a .env file (repo root or the example's directory)."
            )
        for step in on_startup:
            await step()
        yield

    return lifespan


def allowed_hosts() -> list[str]:
    """The Host values the API answers to.

    Locally that is loopback only. Rejecting every other Host header stops DNS-rebinding
    against a demo bound to 127.0.0.1, which CORS does not — the guard is about a process
    on somebody's laptop. A platform that routes to this deployment by host is already
    that front door, and gives each deployment a host name this process cannot know, so
    on one of those the API answers to any host (``DEMO_ALLOWED_HOSTS`` names them
    explicitly, and ``*`` says so outright). A deployment still puts authentication in
    front of these routes: they have none of their own."""
    named = [
        host.strip().rsplit(":", 1)[0] if ":" in host.strip() else host.strip()
        for host in os.environ.get("DEMO_ALLOWED_HOSTS", "").split(",")
    ]
    listed = [host for host in named if host]
    if "*" in listed:
        return ["*"]
    if not listed and hosted():
        return ["*"]
    return ["localhost", "127.0.0.1", *listed]


def build_app(title: str, on_startup: Sequence[Callable[[], Awaitable[None]]] = ()) -> FastAPI:
    """A FastAPI app that answers only to the hosts above and to any localhost origin
    (a deployed frontend is served from the API's own origin, so it needs no CORS grant).
    Logs go to stderr at ``DEMO_LOG_LEVEL``: ``INFO`` is a line per model call, ``DEBUG``
    adds the bodies."""
    logging.basicConfig(
        level=os.environ.get("DEMO_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    # The model-call line carries what httpx's line for the same request would.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = FastAPI(title=title, version="0.1.0", lifespan=_lifespan(on_startup))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


class TurnAgent(Protocol):
    def stream_turn(
        self, messages: list[dict[str, Any]], session: Any, state: Any
    ) -> AsyncIterator[AgentEvent]: ...

    async def update_memory(self, messages: list[dict[str, Any]], session: Any) -> Any: ...


def append_user_turn(record: SessionRecord[Any], message: str, events_label: str) -> None:
    """Add the user's message to the transcript, preceded by a note listing what happened
    outside the conversation since the last reply, when anything did."""
    if not record.pending_app_events:
        record.messages.append({"role": "user", "content": message})
        return
    note = f"[{events_label} since your last reply: " + " ".join(record.pending_app_events) + "]"
    record.pending_app_events.clear()
    record.messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": note}, {"type": "text", "text": message}],
        }
    )


def stream_turn(
    agent: TurnAgent,
    sessions: SessionStore[Any],
    record: SessionRecord[Any],
    session: Any,
    *,
    env_hint: str,
) -> StreamingResponse:
    """Stream one turn as SSE; the record is written back once the stream has ended (the
    request dependency wrote back before it began). Credential failures become a readable
    error event naming ``env_hint`` (where this deployment keeps its key); anything else is
    logged and reported generically. Memory extraction runs after the response has streamed,
    or inside it where nothing runs after one."""

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in agent.stream_turn(record.messages, session, record.state):
                if event.type == "turn_complete" and event.data.get("results_cleared"):
                    record.stored_messages = 0  # earlier messages changed: rewrite the transcript
                yield to_sse(event)
        except anthropic.AuthenticationError:
            logger.exception("chat turn failed: API authentication")
            yield to_sse(
                AgentEvent.error(
                    f"Anthropic API authentication failed (401). Check ANTHROPIC_API_KEY in "
                    f"{env_hint}, unset any stale key exported by your shell, or restart "
                    "with COMMERCE_DEMO_AUTH=sdk to use the SDK's own credential chain."
                )
            )
        except Exception as error:  # the client gets a safe event, the log gets the rest
            logger.exception("chat turn failed")
            described = str(error).lower()
            if any(word in described for word in ("authentication", "credential", "api_key")):
                yield to_sse(
                    AgentEvent.error(
                        "No Anthropic API credentials are configured, so chat can't run. Set "
                        f"ANTHROPIC_API_KEY in {env_hint} and start again; everything except "
                        "chat works without one."
                    )
                )
            else:
                yield to_sse(
                    AgentEvent.error("Something went wrong on our side. Please try again.")
                )
        else:
            # Extraction is the last thing a turn does. Where the process outlives the
            # response it runs after it; where it does not, the stream waits for it.
            if background_outlives_response():
                spawn_background(agent.update_memory(record.messages, session))
            else:
                try:
                    await agent.update_memory(record.messages, session)
                except Exception:  # a memory write must not fail a delivered turn
                    logger.exception("memory extraction failed")

    async def write_back() -> None:
        try:
            await sessions.save(record)
        except SessionConflictError:
            # A button's request wrote the session while the turn streamed. The turn is the
            # larger write, so it goes in over that version; the note the button queued is lost.
            record.version = (await sessions.read_state(record.session_id) or (0, {}))[0]
            logger.warning(
                "session %s: a write raced the turn; the turn wins", session_tag(record.session_id)
            )
            await sessions.save(record)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(write_back),
    )
