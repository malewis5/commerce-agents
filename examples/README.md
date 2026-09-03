# examples

Four vertical demos, each running both agents over one catalog: `retail/` (ACME),
`travel/` (ACME Travel), `telecom/` (ACME Mobile), and `entertainment/` (ACME Tickets).
`python scripts/run_demo.py <vertical>` starts one; each vertical's README lists its ports,
prompts to try on both surfaces, and what it adds to the libraries.

## Layout

| Path | Contents |
|---|---|
| `demo_common/` | Host code the four APIs share: app and middleware (`host.py`), session store (`sessions.py`), storefront routes (`storefront.py`), merchant router (`merchant.py`), memory store, routes, and fixture seeder (`memory.py`), the shared store a deployment keeps its state in (`state.py`) and the mock world it carries (`world.py`), mock-backend helpers (`*_fixtures.py`) |
| `web-shared/` | The npm package the eight web apps import: the API client, the session and turn hooks, the event types (`protocol.ts` mirrors `commerce_common/streaming.py`), the transcript and inspector components, shared primitives and icons, and the two app frames (`storefront/`, `portal/`) |
| `package.json` | The npm workspace: `web-shared` plus every `*/storefront-web` and `*/merchant-web` (`npm ci` installs all of them) |
| `<vertical>/api/` | One FastAPI process: the two mock backends, the two agent configs (`agent_config.py`), the vertical's own routes and presentation extensions, the merchant router mounted under `/api/merchant`, and `index.py`, the entrypoint a deployment loads |
| `<vertical>/vercel.json` | That example as one project of three services — the API, the storefront at `/`, the portal at `/portal` ([`docs/hosting.md`](../docs/hosting.md)) |
| `<vertical>/data/` | The fixtures both backends load, listed in the vertical's README |
| `<vertical>/storefront-web/`, `<vertical>/merchant-web/` | The Next.js apps: this vertical's cards, views, and tokens over `web-shared` |

Sessions, carts, and staged changes live in one process's memory in `demo_common`, so a
local demo runs one worker. Where the environment names a shared store (`state.py`) they
move behind it instead, which is what a deployment of more than one process needs;
`GET /api/health` reports which of the two this process is (`shared_state`). Each mock
declares the state that travels in `world_state`.

`web-shared` holds the session, streaming, and rendering plumbing once. Each app holds its
own components: `components/generative/` has one entry per presentation tool, typed by the
app's `lib/types.ts`, so the four frontends are four builds of the same payload schemas
(`shopping_agent/tools/presentation.py`, `merchant_agent/tools/presentation.py`); a
deployment's frontend is a fifth.

## Showcase pages

Every web app serves `/showcase`, which renders each of its components from
`lib/showcase-fixtures.ts`; it needs neither the API nor a key, and `run_demo.py` prints
the storefront's showcase URL when the demo is up. Open it when changing a component.

## Identity

A storefront session starts by naming a profile from `data/users.json` (`POST /api/session`);
a merchant session binds to the one merchant the process serves. Every later request carries
only the session id, in `X-Session-Id`, and the routes read the principal from it.

## Environment variables

| Variable | Effect | Read in | Default |
|---|---|---|---|
| `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` | Chat credentials; the environment wins over `<vertical>/.env`, which wins over the repo-root `.env` | `demo_common/host.py` | unset (client credential chain) |
| `COMMERCE_DEMO_AUTH` | `sdk` skips the `.env` files and clears the key variables so the client's credential chain is used; `run_demo.py --federated` sets it | `demo_common/host.py` | unset |
| `DEMO_ALLOWED_HOSTS` | Comma-separated Host values the API answers to besides `localhost` and `127.0.0.1`; `*` answers to any, which is what a platform that routes by host itself gets by default | `demo_common/host.py` | unset |
| `DEMO_LOG_LEVEL` | `INFO` writes one line per model call; `DEBUG` adds each request and response | `demo_common/host.py` | `INFO` |
| `MERCHANT_REQUIRE_HOST_APPROVAL` | `0` lets an approval typed in chat apply a change; `1` requires the preview card's button | `demo_common/host.py` | `1` |
| `MERCHANT_ANALYSIS_CODE_EXECUTION` | `1` mounts the hosted code execution tool in the retail analysis delegate | `retail/api/agent_config.py` | `0` |
| `MERCHANT_ANALYSIS_MODEL` | The retail analysis delegate's model | `retail/api/agent_config.py` | unset (main model) |
| `NEXT_PUBLIC_API_URL` | Where a web app sends its requests; `run_demo.py` sets it to the port the API came up on, and a deployment that serves the web apps from the API's own origin needs nothing | `web-shared/api.ts` | `http://localhost:<API_PORT>` |

A deployment reads five more, all of which a local run leaves unset
([`docs/hosting.md`](../docs/hosting.md)):

| Variable | Effect | Read in |
|---|---|---|
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | The Redis-over-HTTP endpoint the sessions, memory, and mock world live in (`UPSTASH_REDIS_REST_*` is read second) | `demo_common/state.py` |
| `COMMERCE_STATE_NAMESPACE` | Key prefix, so several deployments can share one store | `demo_common/state.py` |
| `COMMERCE_STATE_STORE` | `memory` forces the in-process store, `none` forces no store | `demo_common/state.py` |
| `COMMERCE_INLINE_BACKGROUND` | `1` awaits memory extraction inside the turn, for a process that stops when its response ends | `demo_common/host.py` |
| `COMMERCE_REPO_ROOT`, `COMMERCE_DATA_DIR` | Where the skills and the fixtures are, for a bundle that carries a slice of the repository rather than all of it | `demo_common/host.py`, `demo_common/storefront_fixtures.py` |

The API reads its variables at startup; a web app takes the `NEXT_PUBLIC_` values when it
is built or its dev server starts.
