# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DBT OpenClaw UI is a React frontend POC for a Deep Agent Workspace platform that manages K8s-backed AI agent workspaces. The UI communicates with a separate Python gateway backend (not in this repo) running at `localhost:8000`. All UI text is in Traditional Chinese (zh-TW).

## Commands

All commands run from the `frontend/` directory:

```bash
cd frontend
npm install        # install dependencies
npm run dev        # start dev server on http://localhost:3000
npm run build      # production build to frontend/dist/
npm run preview    # preview production build
```

No test framework is configured — there are no tests.

## Architecture

```
frontend/src/
  main.jsx              # ReactDOM entry point
  App.jsx               # Root component — tab-based navigation (no router), role-based tab visibility
  index.css             # Tailwind directives only
  api/client.js         # Gateway API client factory — 18 endpoints (:8000)
  api/adminClient.js    # Admin + Storage Service API client factory — 22 endpoints (via Gateway proxy)
  context/AppContext.jsx # Global state, both API instances, 502/TypeError auto-recovery, admin role state
  components/
    ConnectionPanel.jsx  # Gateway URL/token config + optional Admin Service connection
    WorkspacePanel.jsx   # Workspace ensure/status lifecycle
    ChatPanel.jsx        # AI chat — 3 modes: channel, sync, stream (SSE) + mermaid rendering
    FileManager.jsx      # File tree with upload/download/write (REST + MCP fallback)
    AdminPanel.jsx       # Full admin dashboard — 5 sub-tabs (users, workspaces, pods, channels, reap)
```

### Key Patterns

- **Dark mode**: Tailwind `darkMode: 'class'` strategy. Toggle in header persists preference to `localStorage` key `dbt-openclaw-dark-mode`, with `prefers-color-scheme: dark` as initial fallback. AppContext manages state and syncs `dark` class on `<html>`.

- **Unified login with role-based access**: Single entry point. ConnectionPanel has an optional "Admin Service 設定" section. When Admin Service is connected, `isAdmin` becomes true, the admin tab appears, and a role badge shows in the header. General users never see admin functionality.

- **Dual API clients**: Gateway API (`api/client.js`, `:8000`) for workspace/chat/file operations. Admin + Storage Service API (`api/adminClient.js`, via Gateway proxy `/api/v1/admin/*` and `/api/v1/workspaces/storage/*`) for user/workspace/pod management and storage operations. Both are instantiated in AppContext with the same Gateway base URL but independent tokens.

- **API client** (`api/client.js`): Factory function `createApiClient({ baseUrl, token })` returns an object with methods for all endpoints. Uses native `fetch`, Bearer token auth, custom `ApiError` class with HTTP status. SSE streaming in `chatStream()` parses `content`, `tool_call`, `tool_result`, `file`, and `[DONE]` events.

- **Admin API client** (`api/adminClient.js`): Factory function `createAdminApiClient({ baseUrl, token })` for Admin Service endpoints (#17-28), Storage Service endpoints (#29-37), and Gateway admin endpoints (#18 channels — requires admin token). 22 endpoints total. Same pattern as Gateway client — native `fetch`, Bearer token, `ApiError`. All requests go through Gateway proxy.

- **502/TypeError auto-recovery** (`context/AppContext.jsx`): `withAutoRecover(operation)` wraps async calls — on 502 (pod unreachable) or TypeError (fetch network failure, e.g. pod offline), it calls `ensureWorkspace` then polls `getWorkspace` up to 15 times at 2s intervals until the pod is `active`, then retries the original operation. Concurrent recovery attempts are deduplicated via `useRef`.

- **Tab gating**: Tabs use three conditions — `requiresConnection` (gateway connected), `requiresWorkspace` (pod active), `requiresAdmin` (admin service connected). Admin tabs are hidden (not just disabled) for non-admin users.

- **Chat modes**: `stream` (default, SSE with tool call interleaving), `channel` (gateway-managed with IM commands like `/new`, `/status`), `sync` (simple request/response).

- **SSE stream events**: `content` (AI text chunks), `tool_call` (tool invocation), `tool_result` (tool output), `file` (generated files — images rendered inline via `AuthImage`, documents via `AuthDownloadLink`), `[DONE]` (stream end). SSE comment `: thinking` triggers an `onThinking` callback for UI state. Each tool_call/tool_result is rendered as an individual collapsible block. Content between tool rounds creates separate assistant message blocks.

- **Authenticated file access**: `AuthImage` component fetches images with Bearer token and renders as blob URL. `AuthDownloadLink` component fetches files with Bearer token and triggers browser download. Both avoid direct `<a href>` / `<img src>` which cannot carry Authorization headers.

- **Mermaid diagrams**: AI responses containing ` ```mermaid ` code blocks are rendered as diagrams. Mermaid library is lazy-loaded via dynamic `import()` to avoid impacting initial bundle size. On render failure, falls back to displaying the raw source code and cleans up any DOM remnants.

- **File operations**: FileManager tries REST API first, falls back to MCP JSON-RPC 2.0 format. File upload uses `FormData`. `deleteFile()` in client.js tries DELETE first, falls back to POST on 405 (backend compatibility).

- **History with files**: `apiMessagesToChat()` parses `tool` role messages with `files` array (from session messages API) and renders them as inline images or download links, reusing the same `AuthImage`/`AuthDownloadLink` components.

## Tech Stack

- React 18.3 (JSX, no TypeScript)
- Vite 6 (dev server + build)
- Tailwind CSS 3.4 + @tailwindcss/typography (darkMode: 'class')
- react-markdown + remark-gfm for chat message rendering
- mermaid (lazy-loaded) for diagram rendering in chat
- No client-side router, no state management library, no HTTP client library

## API Reference

Detailed backend API documentation is in `frontend-api.md`:
- Gateway API (`:8000`): 18 endpoints — health, workspace, chat, sessions, files, gateway admin
- Admin Service API (via Gateway proxy `/api/v1/admin/*`): 12 endpoints — user management, workspace management, pod ops, reap
- Storage Service API (via Gateway proxy `/api/v1/workspaces/storage/*`): 9 endpoints — workspace storage CRUD, file ops, access control

## Notes

- The `frontend/dist/` directory is gitignored (not committed). Run `npm run build` to generate it locally.
- Default POC credentials are hardcoded in AppContext:
  - Gateway: token `$POC_STATIC_TOKEN:testuser1`, base URL `http://localhost:8000`
  - Admin Service: token `$POC_ADMIN_TOKEN`
- Admin Service is a separate Pod accessible via Gateway proxy (`/api/v1/admin/*`). No direct connection needed.
- Storage Service is a separate Pod (`:8091`, ClusterIP) accessible via Gateway proxy (`/api/v1/workspaces/storage/*`, `/api/v1/workspaces/{wid}/storage/*`).
- No `.env` files — Vite's `VITE_*` env var convention is available but unused.
- No CI/CD, no Docker configuration.
