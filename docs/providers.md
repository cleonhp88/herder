# Providers — adding any CLI agent

Herder does not hard-code support for individual AI CLIs. It ships **one generic
CLI adapter** (`type: cli`) that turns any command-line agent into a job runner.
**Adding a new CLI is a config change, not a code change** — you describe how to
invoke the binary and how to read its output, and Herder does the rest.

If your CLI exposes a **non-interactive / print mode** (most do: `-p`, `--print`,
`--no-interactive`, `--message`, `exec`, …), Herder can drive it.

---

## How it works

```
prompt ──▶ build argv from config ──▶ run subprocess ──▶ parse stdout ──▶ Result
                (executable + args + input mode)            (parser)
```

The adapter (`src/herder/providers/cli_generic.py`) builds the argument vector
from your provider config, passes the prompt via the configured **input mode**,
runs the subprocess under the resolved environment, and applies the configured
**parser** to stdout. No per-CLI Python code is involved.

---

## Provider field reference

Source of truth: `src/herder/config.py`.

| Field | Type | Default | Notes |
|---|---|---|---|
| `type` | `cli` \| `api` \| `ollama` | — | Use `cli` for command-line agents. |
| `executable` | string | — | Absolute path preferred (avoids `$PATH` collisions). |
| `args` | list[string] | `[]` | Fixed flags. The prompt is appended/piped per `input`. |
| `input` | `stdin` \| `arg` \| `arg_or_stdin` \| `file` | `stdin` | How the prompt reaches the CLI (see below). |
| `env_profile` | string \| null | null | Name of an `env_profiles` entry; controls which env vars leak in. |
| `model` | string \| null | null | Used by `ollama` type; for `cli`, pass the model via `args`. |
| `base_url` | string \| null | null | For `ollama` HTTP type. |
| `timeout` | int (seconds) | `1800` | Hard kill ceiling for one job. |
| `max_concurrency` | int | `1` | Parallel jobs allowed for this provider. |
| `parser` | `text` \| `json:<key>` | `text` | `text` = passthrough; `json:foo` = `json.loads(stdout)["foo"]`. |
| `cost_key` | string \| null | null | Label for cost/stats accounting. |

### Input modes

| Mode | Effect | Use when |
|---|---|---|
| `stdin` | Prompt piped to stdin; `args` unchanged. | CLI reads the prompt from stdin (e.g. `claude -p`). |
| `arg` | Prompt appended as the **last positional arg**. | CLI takes the prompt as a positional/after a trailing flag. |
| `arg_or_stdin` | Same as `arg` (appended positionally). | CLI accepts either; positional is simpler. |
| `file` | Prompt written to a temp file; its **path** appended as last arg. | CLI takes a prompt file path. |

> **`arg` + trailing flag trick:** to produce `tool --message "<prompt>"`, put
> `--message` as the *last* entry in `args` and use `input: arg`. The prompt is
> appended right after it.

## Role field reference

| Field | Type | Default | Notes |
|---|---|---|---|
| `provider` | string | — | Name of a provider defined above. |
| `permissions` | string | `read_only` | Convention: `read_only`, `worktree_write`. |
| `output_format` | string | `report` | Free label consumed by the frontend. |
| `retry_policy` | string | `standard` | Free label consumed by the worker. |
| `default_timeout` | int \| null | null | Overrides provider timeout for this role. |
| `max_concurrency` | int \| null | null | Per-role cap. |
| `system_prompt_file` | path \| null | null | Optional system prompt injected per job. |

---

## Verify before you trust

Every plan-account CLI breaks differently (token expiry, TTY assumptions, plan
gating). **Always probe before wiring a role onto it:**

```bash
# 1. Smoke-test the raw CLI in non-interactive mode FIRST
<cli> <print-flag> "Reply with exactly: OK"

# 2. Add the provider to config.yaml, then let Herder probe it
herder doctor
```

If the smoke test fails (auth, no models, hangs), the provider is not usable yet
— fix the CLI's own login/plan before adding it to Herder.

---

## ACP providers (protocol mode)

For agents that speak the [Agent Client Protocol](https://agentclientprotocol.com)
(JSON-RPC over stdio), `type: acp` replaces stdout-scraping with a structured
session: streamed message chunks, typed stop reasons, and a headless permission
policy (read-only jobs deny all tool requests; write jobs allow with
least-privilege `allow_once`). Requires the optional extra:
`uv pip install herder[acp]` (or `agent-client-protocol>=0.10.1`).

```yaml
providers:
  # opencode over ACP (verified live)
  opencode_acp: { type: acp, executable: /path/to/opencode, args: ["acp"], timeout: 120 }

  # Gemini CLI over ACP
  gemini_acp:   { type: acp, executable: gemini, args: ["--acp"], timeout: 120 }

  # Claude Code via the Zed ACP adapter
  claude_acp:   { type: acp, executable: npx, args: ["@zed-industries/claude-code-acp"], timeout: 120 }
```

Notes: ACP providers cannot run `untrusted` roles (no seatbelt sandbox wrap in
v1 — config load rejects that combination). `herder doctor` probes ACP
providers with a real `initialize` handshake.

## Tested providers

These are verified working with the generic adapter. Copy, adjust paths/models.

```yaml
providers:
  # Claude Code — prompt via stdin
  claude_cli:   { type: cli, executable: claude,  args: ["-p"], input: stdin, timeout: 60, parser: text }

  # OpenAI Codex CLI — exec subcommand, prompt via stdin
  codex_cli:    { type: cli, executable: codex,   args: ["exec", "-m", "gpt-5.4-mini"], input: stdin, timeout: 60, parser: text }

  # Gemini CLI — prompt as positional after -p
  gemini_cli:   { type: cli, executable: gemini,  args: ["--skip-trust", "-m", "gemini-3-flash-preview", "-p"], input: arg_or_stdin, timeout: 60, parser: text }

  # GitHub Copilot CLI — needs GH token via env_profile
  copilot_cli:  { type: cli, executable: /opt/homebrew/bin/copilot, args: ["-p"], input: arg, env_profile: copilot, timeout: 120, parser: text }

  # OpenCode — run subcommand
  opencode_cli: { type: cli, executable: /path/to/opencode, args: ["run", "-m", "<provider>/<model>"], input: arg, timeout: 120, parser: text }

  # Ollama (LAN or local) — HTTP type, no CLI
  ollama_lan:   { type: ollama, base_url: "http://HOST:11434", model: "qwen3.6:27b", timeout: 300 }

env_profiles:
  copilot: { allow_env: [GH_TOKEN, GITHUB_TOKEN] }
```

---

## Community cookbook (UNVERIFIED — check `--help` first)

> ⚠️ The snippets below are best-effort starting points. Flags, model names, and
> auth differ per CLI and per version. **Smoke-test the raw command and run
> `herder doctor` before relying on any of these.** PRs with verified configs +
> a note on the version tested are welcome.

```yaml
providers:
  # Aider — non-interactive via trailing --message (prompt appended after it)
  aider_cli:    { type: cli, executable: aider, args: ["--yes", "--no-auto-commit", "--no-stream", "--message"], input: arg, timeout: 300, parser: text }

  # Cursor Agent CLI — paid plan; ask mode = read-only.
  # NOTE: the `agent` symlink may collide with other tools — use the full path.
  cursor_cli:   { type: cli, executable: /Users/you/.local/bin/cursor-agent, args: ["-p", "--mode", "ask", "--model", "sonnet-4"], input: arg, timeout: 120, parser: text }

  # Kiro CLI (Amazon Q-based) — chat needs a Pro/Identity-Center token,
  # NOT a free Builder ID (free Builder ID cannot chat).
  kiro_cli:     { type: cli, executable: kiro-cli, args: ["chat", "--no-interactive", "--trust-tools=", "--effort", "low"], input: arg, timeout: 90, parser: text }

  # Kimi Code CLI (Moonshot) — needs MOONSHOT_API_KEY
  kimi_cli:     { type: cli, executable: kimi, args: ["--print"], input: arg, env_profile: kimi, timeout: 120, parser: text }

  # Cline — open-source; confirm its non-interactive flag from `cline --help`
  cline_cli:    { type: cli, executable: cline, args: ["--non-interactive"], input: arg, timeout: 180, parser: text }

env_profiles:
  kimi: { allow_env: [MOONSHOT_API_KEY] }
```

**Not recommended to ship as adapters** (paid/enterprise/niche or no stable
non-interactive contract): Factory Droid (enterprise), Auggie (Augment plan),
Qodo Gen, Crush, Kilocode, Devin CLI (unofficial). They still work via the same
generic adapter if you have access — add them with the pattern above.

---

## Gotchas

| Gotcha | Fix |
|---|---|
| **Binary name collisions** (e.g. multiple tools install `agent`) | Use the **absolute path** in `executable`. |
| **CLI needs a secret** | Add an `env_profiles` entry with `allow_env: [THE_KEY]` and set `env_profile`. Env minimization hides everything else. |
| **CLI reads creds from disk** (after `login`) | No env var needed — `HOME` is passed through, so on-disk tokens are found. |
| **Auth looks fine but chat fails** | Identity ≠ entitlement. Confirm the *plan* covers CLI/API usage (e.g. Kiro free Builder ID cannot chat). |
| **Interactive-only CLI** | If there is no print/non-interactive mode, Herder cannot drive it headlessly. |
