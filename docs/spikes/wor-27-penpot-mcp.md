# WOR-27 Spike: Penpot MCP Server Evaluation

**Date:** 2026-04-11
**Status:** COMPLETE — GO (Superdesign MCP)

---

## What is the Penpot MCP Server?

The Penpot MCP server is an official integration that bridges AI assistants (via the Model Context Protocol) with Penpot design files. It allows an LLM to read, modify, and create design elements programmatically through the Penpot Plugin API.

Architecture: the MCP server communicates with Penpot via a browser plugin that connects over WebSocket. The LLM sends MCP tool calls → server forwards them → plugin executes via the Penpot Plugin API in the user's active browser session.

---

## Exposed Tools and Capabilities

### Local MCP (available today)

| Tool | Description |
|------|-------------|
| `execute_code` | Run arbitrary Penpot Plugin API code |
| `high_level_overview` | Return a structural summary of the open design file |
| `penpot_api_info` | Introspect available Plugin API surface |
| `export_shape` | Export a shape/frame as an image or asset |
| `import_image` | Import a local image into the design file |

Plus higher-level query/write operations:
- Query and filter shapes by type, area, color, font, text content
- Get detailed properties of a shape (colors, fonts, effects, layout)
- Create text elements with full styling
- Create new design files, list projects, manage file metadata
- Rename layers, organise components, audit design system consistency

Both **read and write** operations are supported.

### Remote MCP (not yet available)

A token-based remote mode is in development. It uses a personal token generated from the Penpot account integrations page, embedded in the server URL. `export_shape` is limited and `import_image` from local paths is unavailable in this mode.

---

## Self-Hosting Requirements (Penpot)

### Local mode (only option today)

- **Node.js v22** required
- HTTP/SSE endpoint on port 4401
- WebSocket server on port 4402 for plugin connections
- Penpot browser plugin must be installed and active — **the plugin window must remain open during the entire session**
- Authentication via the user's active Penpot browser session (no separate token)

### Remote mode (future)

- **Not yet available on `penpot.app` production** — targeted for Penpot v2.16
- Token-based auth (no active browser session needed)

---

## Intended Use Case

The target workflow is:

```
groom-ticket → Claude generates wireframe → developer reviews/approves → start-ticket → implement
```

Claude creates the wireframe (not the developer). The developer reviews and approves it as part of the groom phase, and implementation only starts after that approval. This means the absence of pre-existing design files is **not** a blocker — the MCP server's **write/generate** capabilities are the relevant surface, not read.

---

## Alternatives Assessed

### Penpot MCP

- **Fit:** Good write capabilities; developer reviews in penpot.app
- **Blocker:** Remote MCP not on penpot.app yet; local mode requires open browser plugin window — incompatible with headless agentic workflow

### OpenPencil MCP

- **Repo:** [open-pencil/open-pencil](https://github.com/open-pencil/open-pencil) — MIT licensed
- **Tools:** 90 MCP tools including full write surface; reads .fig files natively
- **Headless?** HTTP mode (port 3100) is headless; Claude Code integration still requires the desktop app open
- **Status:** "Active development. **Not ready for production use**" — their own words
- **Verdict:** Most capable long-term; blocked by production-readiness today

### Superdesign MCP

- **Repo:** [jonthebeef/superdesign-mcp-claude-code](https://github.com/jonthebeef/superdesign-mcp-claude-code)
- **Tools:** `superdesign_generate`, `superdesign_iterate`, `superdesign_extract_system`, `superdesign_list`, `superdesign_gallery`
- **Setup:** Node.js 16+, no external API key (uses Claude Code's built-in LLM)
- **Headless?** Yes — no browser or desktop app needed
- **Status:** Early-stage (few commits, no versioned release), unclear license
- **Verdict:** Best fit for the intended workflow — designed exactly for prompt-to-wireframe within Claude Code

### Figma-based servers

Require a Figma account. Figma is proprietary SaaS — not open-source-first. Excluded.

---

## Decision: GO — Superdesign MCP

**Integrate Superdesign MCP as the design step in the groom-ticket phase.**

Despite being early-stage, Superdesign is the only option that:
- Is designed for the exact intended workflow (Claude generates → developer reviews)
- Requires no browser window, no desktop app, no external API key
- Works directly within Claude Code via stdio MCP

The early-stage risk is acceptable because this is a developer tooling workflow (not user-facing) and the output (an HTML wireframe) is reviewable and disposable — a bad wireframe is easy to discard or regenerate.

---

## Proposed Workflow Step

Insert a wireframe generation + approval gate inside `groom-ticket`, after scope is agreed and before acceptance criteria are finalised:

```
/groom-ticket WOR-123
  1. PO review: scope, acceptance criteria, splitting
  2. [NEW] Claude generates wireframe(s) via Superdesign MCP for any UI-facing changes
  3. Developer reviews wireframe — approves, requests iteration, or skips (non-UI tickets)
  4. Acceptance criteria updated to reference approved wireframe if applicable
  ↓ human approves — Linear updated only after this
```

Non-UI tickets (pure logic, config, tests) skip step 2–3.

---

## Sources

- [penpot/penpot-mcp (archived)](https://github.com/penpot/penpot-mcp)
- [Penpot MCP official docs](https://help.penpot.app/mcp/)
- [Penpot MCP in main repo](https://github.com/penpot/penpot/tree/develop/mcp)
- [Smashing Magazine: Penpot experimenting with MCP servers](https://www.smashingmagazine.com/2026/01/penpot-experimenting-mcp-servers-ai-powered-design-workflows/)
- [OpenPencil GitHub](https://github.com/open-pencil/open-pencil)
- [Superdesign MCP for Claude Code](https://github.com/jonthebeef/superdesign-mcp-claude-code)
- [Snyk: 14 MCP servers for UI/UX engineers](https://snyk.io/articles/14-mcp-servers-for-ui-ux-engineers/)
