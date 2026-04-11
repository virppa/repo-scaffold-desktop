# WOR-27 Spike: Penpot MCP Server Evaluation

**Date:** 2026-04-11
**Status:** COMPLETE — NO-GO

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

## Self-Hosting Requirements

### Local mode (only option today)

- **Node.js v22** required
- HTTP/SSE endpoint on port 4401
- WebSocket server on port 4402 for plugin connections
- Penpot browser plugin must be installed and active — **the plugin window must remain open during the entire session**
- Authentication via the user's active Penpot browser session (no separate token)
- Optional: `mcp-remote` proxy for stdio transport compatibility

### Remote mode (future)

- **Not yet available on `penpot.app` production** — targeted for Penpot v2.16
- Would work with both penpot.app (cloud) and self-hosted Penpot instances
- Token-based auth (no active browser session needed)

---

## Cloud vs. Self-Hosted Penpot

| | Local MCP | Remote MCP |
|---|---|---|
| `penpot.app` (cloud) | Yes (via browser plugin) | Not yet (v2.16 target) |
| Self-hosted Penpot | Yes | In progress |

You do **not** need to self-host Penpot to use the MCP server today — you can use penpot.app with the local MCP. However, you do need Node.js running locally and a browser window open.

---

## Maturity Assessment

- Pre-beta, active experiment (Penpot's own words)
- Original standalone repo (`penpot/penpot-mcp`) was **archived 2026-02-03** and merged into the main Penpot repo — sign of consolidation, not abandonment
- Known issue: Chromium v142+ has browser connectivity restrictions affecting the plugin
- No stable API guarantees; toolset may change between Penpot releases

---

## Fit Assessment for This Project

This project (`repo-scaffold-desktop`) is a CLI/desktop tool that generates repository skeletons. Key considerations:

| Factor | Assessment |
|--------|------------|
| Does this project have Penpot designs? | No — no wireframes exist yet |
| Is the UI design-heavy? | No — v1 is a minimal PySide6 form |
| Does our agentic workflow run headlessly? | Yes — Claude Code + hooks, no browser |
| Can we keep a browser plugin open during agent runs? | No — incompatible with CI and unattended runs |
| Is Remote MCP available? | No — not until Penpot v2.16 |
| Would Claude reference Penpot during groom/start? | Only if designs existed and were kept in sync |

---

## Decision: NO-GO

**Do not integrate the Penpot MCP server at this time.**

Reasons:
1. **Remote MCP not production-ready** — the only headless-compatible mode is not available on penpot.app yet. Local mode requires an active browser window, which cannot run in our agentic/CI workflow.
2. **No designs to reference** — this project has no Penpot files. Integrating MCP without wireframes adds infrastructure with zero immediate value.
3. **Pre-beta stability** — the API is experimental, Chromium v142+ has known issues, and the toolset is subject to change.
4. **Workflow friction outweighs benefit** — the overhead (run Node.js server, keep plugin open, manage tokens) doesn't fit a fast code-generation workflow.

**Revisit when:**
- Penpot v2.16 ships Remote MCP to `penpot.app` production
- The team starts producing Penpot wireframes for the UI
- The API reaches at least beta stability

---

## Sources

- [penpot/penpot-mcp (archived)](https://github.com/penpot/penpot-mcp)
- [Penpot MCP official docs](https://help.penpot.app/mcp/)
- [Penpot MCP in main repo](https://github.com/penpot/penpot/tree/develop/mcp)
- [Smashing Magazine: Penpot experimenting with MCP servers](https://www.smashingmagazine.com/2026/01/penpot-experimenting-mcp-servers-ai-powered-design-workflows/)
