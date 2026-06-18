# Dashboard (`web/`)

Read-only observability panel for the gate, served by `server/read_api.py`.

## Why server-rendered, not the React SPA (neckbeard decision)

> Neckbeard is the project's minimalism ruleset, **forked from Ponytail** (MIT) and renamed.

The spec names **React 19 + Vite + Tailwind** as the dashboard stack *and* adds a
neckbeard note: *"if the views stay this simple, server-rendered with minimal JS is
acceptable. Apply the ladder."*

Applying the ladder:

1. **Does this need to exist?** A heavy SPA toolchain for a handful of read-only
   tables and cards — not yet. (YAGNI)
2. **Stdlib does it?** Python's `http.server` serves these static assets and the
   JSON API with no dependency to install or build, and it stays verifiable offline.

So the shipped dashboard is **hand-written HTML + one vanilla JS file + one CSS
file**, served from this directory. It is dependency-free, has no build step, and is
fully exercisable in tests against the read API. This is the lean choice the spec
explicitly authorizes, and it demonstrates the neckbeard discipline the gate is built
to enforce.

`neckbeard: server-rendered dashboard instead of a React SPA. Upgrade path — when the
views grow interactive state (filtering, live graphs, drill-downs beyond a modal),
scaffold a React 19 + Vite + Tailwind app here that consumes the same /api/* surface;
the read API and its JSON shapes do not change.`

## Files

- `dashboard.html` — the shell (served at `/`).
- `app.js` — fetches `/api/*` with the `X-Gate-Session-Token` header; renders Live,
  Queue, Audit (with JSON/CSV export), Neckbeard (debt ledger + protected-set
  blocks), Metrics, and the fail-closed banner. Token is held in memory only.
- `styles.css` — dark theme, tier/decision badges.

## Security

The page itself carries no data and needs no token to load. Every `/api/*` call
requires the ephemeral session token (printed in the server's startup banner) in the
`X-Gate-Session-Token` header. The server enforces loopback bind, Host-header
validation, localhost-only CORS, and secret redaction — see `server/read_api.py`.
