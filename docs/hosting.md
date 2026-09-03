# Hosting an example

`python scripts/run_demo.py retail` is one process on one machine, and the examples are
written for that: sessions, carts, and staged changes live in its memory, retail's memory
store is a file beside its fixtures, and memory extraction runs after the reply has been
sent. A hosted deployment is none of those things. This is what changes, what carries the
difference, and what each example README's Deploy button sets up.

Nothing here changes an agent. The prompts, skills, tool contracts, and gates are the same
bytes on a laptop and on a deployment; what moves is where the host keeps state.

## What a deployment is not

| A local run | A deployment |
|---|---|
| One process holds every session, cart, and staged change | Several processes, replaced whenever the platform likes, none of which has seen the others' requests |
| The filesystem is writable and outlives the process | The bundle is read-only, and anything written to `/tmp` goes with the instance |
| Work started after a response finishes runs | A function is frozen when its response ends |
| Requests arrive on loopback | Requests arrive at a public host name the process cannot know in advance |

Each row has one answer in the examples:

- **State** moves behind [`StateStore`](../examples/demo_common/state.py) — sessions
  ([`sessions.py`](../examples/demo_common/sessions.py)), what is remembered
  ([`memory.py`](../examples/demo_common/memory.py)), and the mock world
  ([`world.py`](../examples/demo_common/world.py)). With no store configured every one of
  them falls back to the in-process version, which is why a local run is unchanged.
- **Memory extraction** is awaited inside the turn's stream instead of after it, so the
  last thing a turn does still happens (`background_outlives_response` in
  [`host.py`](../examples/demo_common/host.py)). Retail's file-backed memory store gives
  way to the shared one; where neither is writable it degrades to in-process rather than
  raising.
- **The loopback guard** is a local protection — it stops a page on another origin from
  reaching a demo bound to 127.0.0.1 — so where a platform routes to the deployment by
  host itself the API answers to any host. `DEMO_ALLOWED_HOSTS` names them explicitly.

## The state store

`StateStore` is four shapes, which is what the examples' state actually is:

| Shape | What it holds | Access |
|---|---|---|
| A versioned document | one session's state; the mock world of one deployment | read and write under a compare-and-set |
| An appendable list | one session's transcript | append, or truncate and rewrite |
| A field map | what is remembered about one subject | per-field write and delete |
| A member set | which sessions belong to one user | add and remove |

Two implementations ship. `RedisStateStore` puts the four on Redis over HTTP — the dialect
Upstash serves and the Vercel Marketplace provisions — speaking HTTP rather than the wire
protocol because a function that handles one request has nowhere to keep a connection
pool. `MemoryStateStore` is the same four in a dict, for the tests. A deployment with a
store of its own implements the protocol over that; nothing above the module knows which
one it has.

| Variable | Effect |
|---|---|
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | The Redis endpoint, as the Vercel Marketplace names it |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | The same endpoint, as Upstash names it (read second) |
| `COMMERCE_STATE_NAMESPACE` | Key prefix, so several deployments can share one store |
| `COMMERCE_STATE_STORE` | `memory` forces the in-process store, `none` forces no store |

Keys are `{namespace}:{vertical}:{role}:…`, and session keys expire after a week.
`GET /api/health` reports `shared_state`: false on a deployment means the store is not
wired, which is worth seeing before a demo rather than during one.

## The mock world

Sessions and memory are what any deployment of these agents keeps. The third document is
particular to the examples. The mock backends hold two different things: the fixtures in
`data/`, which are code and identical in every process, and what a demo *does* — a cart
line, a change staged but not approved, a restock that moved a number, an eight-minute
ticket hold. A change is staged by one request and approved by another, and the storefront
has to show the price the portal moved, so that second kind lives in one JSON document per
deployment: read before a request reaches a backend, written back after the response has
gone out, and only when it changed.

Each mock declares its own mutable state (`world_state`), and for catalog records only the
difference from the fixtures travels, so the document is the demo's edits rather than its
data. `merchant_agent.ChangeLedger` and the entertainment example's `TicketingEngine` have
`snapshot`/`restore` for the same reason. A contract test per vertical drives a demo, hands
the document to a second pair of backends built from the fixtures, and checks that the same
reads answer the same way there.

It is one document with a compare-and-set, which is right for a demo world that one person
is driving and wrong for anything else: two processes writing in the same instant end in
last-write-wins with a line in the log. A deployment against real systems has no such
document — each backend method calls the system of record that owns those rows, and that
is the whole of `StorefrontBackend` and `MerchantBackend`
([`docs/backends.md`](./backends.md)).

## The shape on Vercel

One project per example, three services in it
([`examples/retail/vercel.json`](../examples/retail/vercel.json)), everything on one
origin:

| Route | Service | Root |
|---|---|---|
| `/api/*` | the vertical's FastAPI app | `api/` |
| `/portal*` | the merchant portal | `merchant-web/` |
| `/*` | the storefront | `storefront-web/` |

Because both web apps and the API share an origin, the frontends send relative requests
and need no configuration at all (`apiRoot` in `examples/web-shared/api.ts`); the portal
carries `basePath: "/portal"` on a platform build, since that prefix is what its own asset
and link URLs have to have. Retail's listing photos come from the storefront's own
`public/`, which is where they live: locally the API serves them, because there it is the
one thing both web apps can reach.

The API service builds with two scripts:

- [`scripts/vercel/install-api.sh`](../scripts/vercel/install-api.sh) installs the five
  packages an example API imports as built wheels, from these directories, with
  `requirements.txt` as the constraint file — so the pins stay the one pin list and the
  bundle carries no Agent SDK tree it never loads.
- [`scripts/vercel/bundle_api.py`](../scripts/vercel/bundle_api.py) copies the slice of
  the repository the function needs into `api/_deploy/`, in the repository's own layout:
  the shared host code, the vertical's fixtures, and both roles' skill files. A platform
  bundles a service's own directory and nothing above it. `api/index.py` points
  `COMMERCE_REPO_ROOT` and `COMMERCE_DATA_DIR` at that copy and imports the app, which is
  the resolution `uvicorn retail.api.main:app --app-dir examples` already gives locally.

Run either by hand to see what a deployed function carries.

## The Deploy button

Each example README has one. It clones the repository into the reader's account, creates
the project with the example directory as its root, asks for `ANTHROPIC_API_KEY`, and
offers to create the Redis store the state store reads. Everything else the deployment
needs it works out for itself.

What a reader gets is the demo, not a product. The routes have no authentication, the
profiles in `data/users.json` are the only identities, `checkout` renders the cart rather
than taking a payment, and every merchant write is staged until someone approves it.
Deployment protection (on by default for a new project's preview URLs) is the only thing
in front of it; keep it on, or put your own authentication ahead of `POST /api/session`,
which is the one place a principal enters a session
([`docs/safety.md`](./safety.md)).

Chat is the one cost: turns bill against the key. A model call takes as long as it takes,
so the API service sets `maxDuration` to 300 seconds.

## Somewhere else

The three seams above are all a different platform needs:

1. Implement `StateStore` over your store, or point the variables at any endpoint that
   serves the Upstash REST dialect, and pass it to `deployment_state_store`'s callers.
2. Set `DEMO_ALLOWED_HOSTS` to the host names the API answers to, or `*` when something
   in front of it already decides that.
3. Set `COMMERCE_INLINE_BACKGROUND=1` where a process does not outlive its response, and
   `COMMERCE_REPO_ROOT`/`COMMERCE_DATA_DIR` where the deployed tree is a slice of the
   repository rather than the whole of it.

A long-running container needs none of them: it is the local shape, and one worker, as
[`examples/README.md`](../examples/README.md) says. What it does not have is more than one
of itself.

For pointing the runtimes at a model platform other than the Anthropic API — Vertex AI,
Bedrock, Foundry, a gateway — see [`docs/deployment.md`](./deployment.md), which is the
other axis entirely.
