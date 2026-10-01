# ADR 0016: Streamlit for the first UI

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Spec §20 and §34 ask for a minimal UI first: an employee view (chat, responses, citations, activity, pending approvals, references) and a manager view (pending approvals, approve or reject with a comment). §42 asks to record "Why Streamlit initially?". A React/Next.js frontend is a later enhancement.

## Decision
- Build the UI with **Streamlit** (`apps/ui/streamlit_app.py`), in Python, as a pure client of the HTTP API through `aegisdesk.ui.client.ApiClient` (httpx).
- The UI imports nothing else from the backend; a test enforces it.
- Streamlit and its dependencies live in a separate `ui` dependency group. The API image does not carry them.

## Why
- **Time goes to the backend, where the learning is.** A chat view with streaming status, tabs, forms and tables takes about 250 lines. There is no build step, no JavaScript toolchain and no API client generation.
- **It forces the right boundary.** Because the UI only speaks HTTP, anything it can do, a React app, a Slack bot or a test can do too. The backend is not shaped around a widget framework.
- **It is testable.** `streamlit.testing.v1.AppTest` runs the script headless with a stub client, so UI behaviour is covered in CI without a browser.

## Consequences
- Streamlit reruns the whole script on every interaction. That is fine for a demo UI; it is not a model for a high-traffic product UI.
- Session state is per browser tab and in memory: a page reload starts a new conversation view. The conversation itself is safe in the API's checkpointer.
- Identity is a selectbox of synthetic employees that sets `X-Employee-Id`, a stand-in for the gateway (ADR 0015). A real UI would sit behind SSO.
- Replacing it later means writing a new client of the same API; nothing in the backend changes.
