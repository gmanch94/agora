# ADR-0015: Staff Console UI — HTMX + Jinja2 (no Node toolchain)

## Status

Accepted (2026-05-04). First slice ships in this PR; revisited if interactions outgrow HTMX.

## Context

PRD-05 specifies a staff console as the only UI in the prototype — the human-in-the-loop surface for every state transition. The PRD has lived for some time without a UI shell; today the FastAPI app exposes JSON endpoints (`/sagas`, `/sagas/{id}`, `/sagas/{id}/approve`, `/sagas/{id}/compensate`, `/sagas/{id}/reject`, `/sagas/{id}/discover`) but no HTML surface.

Two open questions blocked the start: framework choice (HTMX + Jinja2 vs React + Tailwind/shadcn) and visual fidelity (`DESIGN.md` is a Mistral-AI brand kit — atmospheric sunset gradients, paid display fonts, photography-anchored heroes; rich relative to a research-prototype workflow tool).

The user explicitly authorized HTMX + Jinja2 for the first slice ahead of an autonomous build window. This ADR captures the choice and its trade-offs.

## Decision

Build the staff console as **server-rendered HTML via FastAPI's `Jinja2Templates`**, with **HTMX** layered in for partial re-renders on state-changing actions (Approve / Reject / Compensate / Discover). No client-side framework, no Node toolchain, no separate dev server.

- Templates live at `src/agora/api/templates/` (`base.html`, `inbox.html`, future `detail.html`).
- Static assets live at `src/agora/api/static/` (`theme.css`, future `htmx.min.js` cached locally).
- A new route `GET /` returns the inbox view; existing JSON endpoints keep their shape so the saga lifecycle tests remain unchanged.
- Theme CSS transcribes the subset of `DESIGN.md` tokens the inbox needs as CSS custom properties (`--color-primary`, etc.) — no full 773-line spec port.

## Consequences

**Positive**
- **Zero new build pipeline.** No `npm install`, no `vite`, no `tsc`. The staff console deploys as part of the existing FastAPI image.
- **One process, one repo, one CI.** Triple-gate (`pytest` + `ruff` + `mypy --strict`) covers the UI's tests directly. No separate frontend test runner.
- **HTMX matches the saga's interaction model well.** State transitions are server-driven and idempotency-keyed; partial HTML re-renders fit naturally onto the existing `POST /sagas/{id}/approve` shape.
- **FastAPI-native.** `Jinja2Templates` is a documented integration point; adding it is a few lines.
- **Reversible.** If interactions outgrow HTMX (drag-drop, rich client-side state, real-time multi-user views), a follow-up ADR can introduce a React island for the affected surface without rewriting the rest.

**Negative / accepted trade-offs**
- **DESIGN.md fidelity is partial.** The brand kit's signature sunset gradients, atmospheric photography, and the closing "sunset stripe" are deferred — they're marketing-surface visual language, not workflow-critical. The first slice prioritizes legible information density (table of pending sagas, action buttons) over hero atmospherics.
- **`PP Editorial Old` is paid.** The display font is licensed via Pangram Pangram and not freely loadable. The first slice falls back to Georgia / Cormorant Garamond (system / Google Fonts), accepting a visual gap. A future slice may host the font once licensing is acquired.
- **`Inter` and friends loaded from Google Fonts.** Adds an external CDN dependency at page load; acceptable for a prototype, would need self-hosting for a privacy-strict deployment.
- **No client-side validation today.** Form errors round-trip through the server. Acceptable at prototype-form-volumes; HTMX handles the re-render without a page flash.
- **HTML cannot fully express DESIGN.md's design tokens.** A few component variants (`card-cream`, `card-cream-soft`, `button-on-cream`) only ship for the screens that need them; the rest stay deferred until a screen calls for them. Don't pre-port the whole vocabulary.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **React + Vite + Tailwind + shadcn/ui** | Highest visual fidelity to `DESIGN.md` and best long-term ergonomics for complex client state, but introduces a Node toolchain, a separate dev server, a separate build, and a separate CI consideration. Excessive for a prototype where the saga's interaction model is fundamentally server-driven. Reserve for if/when client-side complexity actually arrives. |
| **Static HTML + a sprinkle of vanilla JS** | Simplest possible, but loses HTMX's `hx-post` / partial-render ergonomics — every action would re-render the whole page or re-implement what HTMX gives us for free. |
| **Server-side templates + full-page POST/redirect** | Works without HTMX, but re-rendering the whole inbox after every Approve click feels jankier than necessary, and the prototype's lifecycle is exactly the shape HTMX is good at (one row mutates, the rest stays). |
| **Mount a separate `frontend/` SPA project** | Dual-deploy complexity. Rejected at the same scope as React above. |

## Implementation notes

**This PR (first slice):**
- Adds `jinja2` as an explicit dependency in `pyproject.toml`. (Already pulled transitively by FastAPI's optional extras, but stating it explicitly avoids surprise.)
- Wires `Jinja2Templates` + `StaticFiles` into `create_app()` in `src/agora/api/app.py`.
- Adds `GET /` returning `inbox.html` populated with a list of non-terminal sagas (re-uses the same query as `GET /sagas`).
- Ships `templates/base.html`, `templates/inbox.html`, `static/theme.css`.
- One ASGI smoke test in `tests/test_staff_console.py` covering: empty inbox renders 200 / `text/html`, populated inbox lists the saga's title and state.
- Doc-count gate auto-bumped via `make sync-doc-counts`; ADR count bumps from 14 → 15.

**Deferred to follow-up PRs:**
- Approve / Reject / Compensate / Discover HTMX endpoints (the existing JSON endpoints stay; HTML form actions land next).
- Detail view (`GET /sagas/{id}` HTML variant rendering the full ledger timeline).
- Authentication (PRD-05 says HTTP basic / dev token acceptable; not in scope here).
- Atmospheric gradients, custom photography, sunset-stripe footer (visual polish).
- Self-hosted fonts.

**Revisit triggers:**
- Drag-and-drop or rich multi-pane state arrives in a screen.
- A surface needs real-time multi-user updates (server-sent events / WebSockets workable but ergonomically ugly through Jinja).
- Brand fidelity becomes load-bearing (e.g. a public-facing landing page that wants the full DESIGN.md sunset treatment).

A revisit lands as a new ADR; this one stays as the rationale for what shipped first.
