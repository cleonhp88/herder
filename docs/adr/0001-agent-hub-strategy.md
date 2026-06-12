# ADR 0001 — Agent-hub strategy: generic-CLI core + optional ACP adapter

- **Status:** Accepted
- **Date:** 2026-06-12
- **Context owner:** Herder maintainers

## Context

Herder's goal is to be a **hub**: plug in any vendor's CLI AI agent and run it as a
background job. The temptation is to chase breadth by hard-coding support for every
CLI. Research (official docs, 2025–2026) shows that is the wrong axis of effort.

Three findings shaped this decision:

1. **The adapter problem is already solved.** Every serious CLI agent exposes a
   non-interactive mode (`-p`, `exec`, `run`, `--no-interactive`, `--message`).
   Herder's generic `cli` adapter — *spawn process, pass prompt via stdin/arg/file,
   capture stdout + exit code* — already drives all of them. This is the universal
   floor and must stay the default.

2. **There is an emerging standard for *driving* an agent: ACP.**
   The [Agent Client Protocol](https://agentclientprotocol.com) (Zed, Apache-2.0,
   JSON-RPC over stdio, stable protocol version 1, announced 2025-08) standardizes
   exactly the host↔agent surface we would otherwise reverse-engineer per CLI:
   `session/prompt`, streaming `session/update`, `session/request_permission`,
   `session/load` (resume), `fs/*` and `terminal/*` callbacks, cancellation.
   Adoption is multi-vendor: **Gemini CLI** native (`--experimental-acp`),
   **opencode** native (`opencode acp`), **Claude Code** via the
   `@zed-industries/claude-code-acp` adapter; Cursor, Codex, and Copilot appear in
   Zed's external-agent list. Clients include Zed (native), JetBrains (native),
   VS Code, Neovim, Emacs.

3. **The other two protocols are the wrong layer.**
   - **A2A** (Agent2Agent, Linux Foundation) is a *peer-to-peer agent mesh* —
     agents discovering and delegating to each other across a network. Not a
     supervisor driving one local CLI.
   - **MCP** (Model Context Protocol) exposes *tools into* an agent. It is the
     opposite direction of "drive an agent" and composes orthogonally with ACP.

A note on de-facto convergence: Claude/Codex/Cursor/opencode all converge on
`-p --output-format json|stream-json`, **but the JSON field names differ per CLI** —
there is no shared output schema. Screen-scraping each CLI's JSON is therefore O(N)
maintenance that grows and silently breaks on version drift.

## Decision

Adopt a **hybrid, generic-CLI-first** architecture:

1. **Keep the generic `cli` adapter as the universal floor.** Any CLI with a
   non-interactive mode plugs in via config (see [providers.md](../providers.md)).
   This covers everything, including non-ACP runners (Ollama, local tools, niche
   CLIs).

2. **Add capability declaration to make "plug-and-run" real.** Each provider
   declares — declaratively, in config — its output format, supported job modes,
   cost hint, and auth env var. The supervisor validates a job against the
   provider's declared capabilities *before* spending a run, and `doctor` probes
   liveness. Pattern borrowed from LiteLLM `model_info` and VS Code `contributes`.

3. **Add ONE optional `protocol: acp` adapter as the ceiling, never a dependency.**
   For agents that ship an ACP server, a single JSON-RPC/stdio client replaces N
   bespoke JSON parsers and unlocks drift-resistant streaming, permission handling,
   and session/cost metadata. A provider opts in with `mode: acp`; absent that, it
   uses the generic path.

4. **Do not build an A2A adapter or an RBAC/policy engine.** Wrong layer / overkill
   for a local supervisor. Revisit A2A only if jobs must call each other as remote
   peers.

## Consequences

**Positive**
- "Plug any CLI in" is delivered by the *floor* (generic adapter + cookbook), so the
  hub works today for every agent, not just standard-compliant ones.
- ACP is upside, not risk: it is opt-in per provider and never required, so its
  youth (v1, remote transport WIP, Claude only via adapter) cannot break the hub.
- Maintenance for ACP-capable agents collapses from N parsers to one client.
- Capability manifests turn onboarding into a config edit + `doctor` probe.

**Negative / costs**
- Two code paths to maintain (generic + ACP) once Tier 3 lands.
- CLI error taxonomy (exit code + stderr → retryable/fatal) is fuzzier than HTTP
  status codes — typed retry/cooldown (roadmap Tier 2) carries real implementation
  risk.
- Capability manifests are only as honest as the maintainer keeps them; a stale
  manifest gives false confidence. Mitigated by `doctor` smoke-tests.

## Alternatives considered

| Alternative | Verdict | Why |
|---|---|---|
| **Per-CLI config only** (status quo, no protocol) | Rejected as the *end state* | Works today but compounds O(N) parser debt and bets against the standard. Kept as the *floor*, not the ceiling. |
| **Single ACP adapter only** | Rejected | Strands every non-ACP runner (Ollama, local tools, first-party-only CLIs) and ties the hub to a v1 protocol with an unfinished remote story. |
| **A2A adapter** | Rejected | Solves networked agent-to-agent collaboration, not a supervisor running local CLI jobs. |
| **Hard-code 15 CLIs into shipped config** | Rejected | 15 auth flows + 15 version-drift sources; most users have access to a handful. Replaced by generic adapter + cookbook docs. |

## Sources

- ACP — https://agentclientprotocol.com , https://agentclientprotocol.com/protocol/overview , https://zed.dev/docs/ai/external-agents
- Gemini ACP — https://geminicli.com/docs/cli/acp-mode/ ; opencode ACP — https://opencode.ai/docs/acp/ ; Claude ACP adapter — https://github.com/zed-industries/claude-agent-acp
- A2A — https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project ; MCP boundary — https://modelcontextprotocol.io/docs/getting-started/intro
- Headless JSON contracts — https://code.claude.com/docs/en/headless , https://developers.openai.com/codex/noninteractive , https://cursor.com/docs/cli/headless
- Prior art — https://github.com/BerriAI/litellm (routing, model_info), https://openrouter.ai/docs/guides/routing/model-fallbacks , https://github.com/dagger/container-use (worktree isolation), https://github.com/agentgateway/agentgateway , https://github.com/agentsmd/agents.md
