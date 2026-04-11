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

## Intended Use Case

The target workflow is:

```
groom-ticket → Claude generates wireframe via MCP → developer reviews/approves → start-ticket → implement
```

Claude creates the wireframe (not the developer), the developer reviews and approves it as part of the groom phase, and implementation only starts after that approval. This means the absence of pre-existing design files is **not** a blocker — the MCP server's write/generate capabilities are the relevant surface, not read.

This framing makes two tools more relevant than initially assessed:

- **Superdesign MCP** — designed exactly for this: generate UI from a natural language spec, iterate, extract a design system. No API key, no browser required.
- **Penpot/OpenPencil write tools** — can programmatically create frames, components, and layouts that the developer then views in the design tool UI.

## Fit Assessment for This Project

| Factor | Assessment |
|--------|------------|
| Does this project need pre-existing designs? | No — Claude generates them as part of groom |
| Is the workflow headless-compatible? | Needs to be — CI and unattended runs |
| Is Penpot Remote MCP available? | No — not until Penpot v2.16 |
| Can Penpot local MCP run headlessly? | No — requires open browser plugin window |
| Is OpenPencil production-ready? | No — self-declared |
| Is Superdesign mature enough? | No — 10 commits, no releases, unclear license |

---

## Free Alternatives Evaluated

Since Penpot MCP isn't viable today, two other free/open-source design MCP options were assessed:

### OpenPencil MCP

- **Repo:** [open-pencil/open-pencil](https://github.com/open-pencil/open-pencil) — MIT licensed
- **Tools:** 90 MCP tools (shape creation, fill/stroke, auto-layout, components, variables, export, design token analysis)
- **Setup:** `bun add -g @open-pencil/mcp`; HTTP mode runs headlessly on port 3100
- **Reads .fig files** natively — Figma files work without a Figma account
- **Headless?** HTTP mode is headless; Claude Code integration still requires the desktop app open
- **Status:** "Active development. **Not ready for production use**" — their own words
- **Verdict:** Most capable alternative long-term; blocked by production-readiness today

### Superdesign MCP

- **Repo:** [jonthebeef/superdesign-mcp-claude-code](https://github.com/jonthebeef/superdesign-mcp-claude-code)
- **Tools:** 5 tools: `superdesign_generate`, `superdesign_iterate`, `superdesign_extract_system`, `superdesign_list`, `superdesign_gallery`
- **Use case:** Generates designs from natural language prompts — does not read existing design files
- **Setup:** Node.js 16+, no external API key (uses Claude Code's built-in LLM)
- **Headless?** Yes — no browser or desktop app needed
- **Status:** 10 commits, no releases, unclear license — highly experimental
- **Verdict:** Different use case (generate, not read); too immature for adoption

### Figma-based servers

Multiple Figma MCP servers exist (Framelink, community servers). All require a Figma API token. Figma has a free tier but it is a proprietary SaaS product. Not suitable for an open-source-first project or for teams without Figma accounts. Excluded.

---

## Decision: NO-GO (all options)

**Do not integrate any design MCP server at this time.**

| Option | Blocker |
|--------|---------|
| Penpot MCP | Remote MCP not on penpot.app yet; local mode requires open browser window |
| OpenPencil MCP | Self-declared not production-ready; desktop app required for Claude Code integration |
| Superdesign MCP | 10 commits, no releases, unclear license, different use case |
| Figma MCP servers | Proprietary SaaS; not open-source-first |

The intended workflow (Claude generates wireframe → developer reviews → implementation starts) is viable in principle — but no option is mature enough to support it reliably today.

**Revisit when:**
- Penpot v2.16 ships Remote MCP to `penpot.app` production (best fit: headless, token-based, cloud)
- OR OpenPencil reaches a stable release (best fit if .fig compatibility matters)
- OR Superdesign MCP cuts a versioned release with a clear license (best fit for prompt-to-wireframe with no extra tooling)
- The chosen server's API reaches at least beta stability

---

## Sources

- [penpot/penpot-mcp (archived)](https://github.com/penpot/penpot-mcp)
- [Penpot MCP official docs](https://help.penpot.app/mcp/)
- [Penpot MCP in main repo](https://github.com/penpot/penpot/tree/develop/mcp)
- [Smashing Magazine: Penpot experimenting with MCP servers](https://www.smashingmagazine.com/2026/01/penpot-experimenting-mcp-servers-ai-powered-design-workflows/)
- [OpenPencil GitHub](https://github.com/open-pencil/open-pencil)
- [Superdesign MCP for Claude Code](https://github.com/jonthebeef/superdesign-mcp-claude-code)
- [Snyk: 14 MCP servers for UI/UX engineers](https://snyk.io/articles/14-mcp-servers-for-ui-ux-engineers/)
