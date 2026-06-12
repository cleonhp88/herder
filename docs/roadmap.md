# Herder Roadmap — toward a universal agent hub

Direction set by [ADR 0001](adr/0001-agent-hub-strategy.md): **generic-CLI core +
optional ACP adapter**. The generic adapter (the floor) already works; this roadmap
adds the layers that make "plug any CLI agent in" reliable and self-describing.

Ranked by leverage / effort. Ship Tier 1 before Tier 2 before Tier 3.

---

## Tier 1 — Self-describing providers (cheap, highest leverage)

The point: onboarding a new agent becomes *edit config + run `doctor`*, with the
supervisor able to reject impossible jobs before spending a run.

- [ ] **Capability manifest on `Provider`.** Add declarative fields:
  `output_format` (`text` | `json` | `stream-json`), `supports` (e.g.
  `[read_only, write, plan]`), `cost_hint`, and an explicit `auth_env` reference.
  Source pattern: LiteLLM `model_info`, VS Code `contributes`.
- [ ] **`extra="forbid"` on all config models** (`Provider`, `Role`, `Project`, …).
  Today a typo'd key is silently swallowed; with a capability manifest that is
  dangerous (false confidence). Fail loud on unknown keys.
- [ ] **Pre-call capability check.** Before dispatch, assert the job's required mode
  ∈ `provider.supports`; reject early with a clear error instead of a failed run.
- [ ] **`doctor` reports the manifest**, not just liveness — show declared
  output_format / supports / auth status per provider so users see what they wired.

*Risk: low. Manifests must be kept honest — `doctor` smoke-tests are the guard.*

## Tier 2 — Reliability for a long-running supervisor

The point: a flaky or rate-limited backend must not stall the queue or fail a job
that another backend could serve.

- [ ] **Role → ordered backend list (fallback).** Today `Role.provider` is a single
  string. Allow a list; the worker walks it by `order` on retryable failure.
  Source: LiteLLM `fallbacks` + OpenRouter model-fallback arrays.
- [ ] **Typed retry by error class.** Map CLI exit code + stderr signature →
  `retryable` vs `fatal`. Don't burn retries on missing-API-key / auth errors; do
  retry transient (timeout, rate limit). Makes the existing `retry_policy` label
  real. *Risk: medium — CLI error taxonomy is fuzzier than HTTP status codes.*
- [ ] **Per-backend cooldown + health isolation.** `allowed_fails` per minute →
  `cooldown_time`; sideline a failing backend without poisoning other roles.
  Source: LiteLLM cooldown model.

## Tier 3 — ACP adapter (the standard ceiling, opt-in)

The point: for agents that speak ACP, replace N bespoke JSON parsers with one
drift-resistant client that gets streaming, permissions, and session/cost for free.

- [ ] **`protocol: acp` provider mode.** A JSON-RPC-2.0-over-stdio client:
  `initialize` (capability negotiation) → `session/new` → `session/prompt`, consume
  `session/update` stream, answer `session/request_permission`, support
  `session/cancel` and `session/load` (resume).
- [ ] **Wire the native speakers first:** Gemini (`gemini --experimental-acp`),
  opencode (`opencode acp`), Claude (`claude-code-acp` adapter). Validate against
  the generic path's output for parity.
- [ ] **Keep it opt-in and non-load-bearing.** A provider without `mode: acp` uses
  the generic `cli` path unchanged. ACP must never become a hard dependency
  (protocol is v1; remote transport is WIP; Claude is adapter-only).

## Explicitly out of scope

- **A2A adapter** — peer-to-peer agent mesh; wrong layer for a local CLI supervisor.
- **RBAC / CEL policy engine** (cf. agentgateway) — overkill for a local tool.
- **LLM-classifier routing** — roles are given on the job; no need to *infer* them.

## Free-ride (no work required)

- **AGENTS.md** is read by every major CLI agent (OpenAI, Anthropic, Google, AWS
  backing; 60k+ repos). Herder passes the job's `cwd` through, so per-repo project
  context is declared once and understood by all backends — no per-agent
  re-declaration. (Note: Claude reads `CLAUDE.md`; a `@AGENTS.md` import bridges it.)

---

*Sources and rationale: see [ADR 0001](adr/0001-agent-hub-strategy.md).*
