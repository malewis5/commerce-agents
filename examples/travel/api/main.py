# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""ACME Travel example API: the mock travel platform behind the shared storefront routes,
with the ``present_itinerary`` extension, and the supplier router under /api/merchant.

    uvicorn travel.api.main:app --app-dir examples --reload --port 8001
"""

from __future__ import annotations

from demo_common import (
    REPO_ROOT,
    MemorySeeder,
    build_storefront_host,
    load_demo_env,
    memory_store_for,
    seed_marker,
)
from shopping_agent_runtime import ShoppingAgent

from .agent_config import build_shopping_config
from .itinerary import build_itinerary_extension
from .merchant import create_merchant_router
from .mock_travel import DATA_DIR, MockTravel

load_demo_env(DATA_DIR.parent)

backend = MockTravel()
agent = ShoppingAgent(
    backend=backend,
    skills_dir=REPO_ROOT / "shopping-agent" / "skills",
    config=build_shopping_config(),
    memory_store=memory_store_for(prefix="travel:shopper"),
    extra_presentation_tools=[build_itinerary_extension()],
)
host = build_storefront_host(
    title="ACME Travel demo API",
    example_root=DATA_DIR.parent,
    backend=backend,
    agent=agent,
    memory_seeder=MemorySeeder(
        DATA_DIR / "memory-seed.json", marker=seed_marker(None, prefix="travel:shopper")
    ),
)
app = host.app
app.include_router(
    create_merchant_router(backend, memory_store_for(prefix="travel:merchant"), world=host.world),
    prefix="/api/merchant",
)
