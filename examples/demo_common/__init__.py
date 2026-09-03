# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Host code the four vertical examples share: the app and its middleware, the session
store both roles use, the storefront routes, the merchant router, the shared store a
deployment keeps its state in (``state.py``, ``world.py``), and (in
``storefront_fixtures`` and ``merchant_fixtures``) the helpers the mock backends call. A
vertical's ``api/`` package constructs its backends, agents, and configs, mounts these,
and adds only its own routes. This module exports what the verticals and their tests
import; the fixture helpers are imported from their own modules."""

from .host import REPO_ROOT, host_approval_default, load_demo_env, spawn_background
from .memory import MemorySeeder, json_file_memory_store, memory_store_for, seed_marker
from .merchant import MerchantIdentity, build_merchant_router
from .sessions import (
    SESSION_HEADER,
    DurableSessionStore,
    SessionConflictError,
    SessionRecord,
    SessionStore,
    UnknownSessionError,
    session_dependency,
    session_store,
)
from .state import MemoryStateStore, StateStore, deployment_state_store
from .storefront import CartAddRequest, StorefrontHost, build_storefront_host
from .world import (
    DurableWorld,
    WorldState,
    cart_lines_state,
    catalog_state,
    declared_once,
    ledger_state,
)

__all__ = [
    "REPO_ROOT",
    "SESSION_HEADER",
    "CartAddRequest",
    "DurableSessionStore",
    "DurableWorld",
    "MemorySeeder",
    "MemoryStateStore",
    "MerchantIdentity",
    "SessionConflictError",
    "SessionRecord",
    "SessionStore",
    "StateStore",
    "StorefrontHost",
    "UnknownSessionError",
    "WorldState",
    "build_merchant_router",
    "build_storefront_host",
    "cart_lines_state",
    "catalog_state",
    "declared_once",
    "deployment_state_store",
    "host_approval_default",
    "json_file_memory_store",
    "ledger_state",
    "load_demo_env",
    "memory_store_for",
    "seed_marker",
    "session_dependency",
    "session_store",
    "spawn_background",
]
