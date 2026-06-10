# Herder

A local job supervisor that runs your CLI AI agents (Claude, Codex, Gemini, Ollama, …) as background jobs.

**Status:** macOS-first · Python 3.12 · SQLite · 285 tests

## What & Why

You have multiple AI CLIs behind subscription/plan accounts. Herder turns them into a unified background worker queue—you enqueue jobs with a role (planner, coder, reviewer, etc.), Herder routes the job to the right provider, runs it, and persists the result. Designed as the backend for Claude Code and other interactive frontends: decouple the "ask for work" from "execute work" so you can retry, schedule, sandbox, and audit AI jobs locally.

## ⚠️ Caveats

**macOS-first.** The security model relies on macOS `seatbelt` sandbox and `launchd` daemon. Linux support is **experimental** (PRs welcome).

**Plan-account CLIs are not designed to be daemonized.** Auth tokens expire, TTY assumptions break, rate limits vary. Always run `herder doctor` first to probe which providers work in your environment.

**`config.yaml` runs the executables you configure.** NEVER run a `config.yaml` you didn't write. It can invoke arbitrary commands under your user.

## Install

```bash
git clone https://github.com/your-org/herder.git
cd herder
uv sync
```

Requires [uv](https://docs.astral.sh/uv/).

## Quickstart

```bash
# Copy the example config and edit to point to your CLI executables
cp config.real.example.yaml config.yaml
# Then edit config.yaml with your actual CLI paths and roles

# Probe which providers work in your environment
uv run herder --config config.yaml doctor

# Enqueue a job
echo "Summarize the latest AI news in 3 bullets" > task.md
uv run herder --config config.yaml enqueue \
  --project example_project \
  --role planner \
  --kind research \
  --prompt-file task.md

# Run pending jobs (once, then exit)
uv run herder --config config.yaml worker --once

# See status
uv run herder ps

# Read the result
uv run herder result <job_id>
```

## Commands

| Command | Purpose |
|---------|---------|
| `doctor` | Probe provider health; test which CLIs are available and reachable. |
| `enqueue` | Enqueue a job to the queue (role + project + prompt). |
| `ps` | List all jobs and their status. |
| `inspect` | Show detailed info about a job. |
| `worker` | Run a daemon (or `--once`) that claims and executes jobs. |
| `result` | Fetch the result.md from a completed job. |
| `tail` | Stream job output in real-time. |
| `cancel` | Mark a job as cancelled. |
| `retry` | Retry a failed job. |
| `approve` | Mark a job as approved (if requires_approval=true). |
| `reject` | Reject a job. |
| `schedules` | List scheduled jobs (cron). |
| `stats` | Show job counts, queue depth, provider latency stats. |
| `gc` | Garbage-collect old jobs and logs. |
| `bench` | Benchmark provider latency and cost. |

## Concepts

**Providers.** Adapters for different AI backends: CLI (claude, codex, gemini, ollama, …) or Ollama HTTP. Config: executable, args, input mode (stdin, arg, env), parser (text, json, codex_exec_json).

**Roles.** Map to a provider + permissions + output format + retry policy. Examples: planner (read_only), coder (worktree_write, code output), reviewer (read_only).

**Projects.** A root directory (or git worktree) + workspace mode (readonly, inplace, worktree). Restrict which roles are allowed per project.

**Jobs.** SQLite queue. Each job is claimed atomically, leased for N seconds (heartbeat to extend), output written to result.md on completion. Support for approval gates and retries.

**Scheduler.** Cron-based scheduling. Tick every N seconds, spawn matching scheduled jobs.

**Stats & Bench.** Aggregate job counts, queue depth, provider latency (p50/p99), cost per provider.

## Security Model

**Environment minimization.** Each provider and role specifies `allow_env`: a whitelist of environment variables exposed. Default is empty; secrets must be explicitly allowed.

**Secret access gating.** Jobs tagged with `secret_keys: [api_key_name]` fail unless the worker has been explicitly unblocked for that secret (via CLI flag or approval).

**Seatbelt sandbox (macOS untrusted preset).** Deny network access, confine writes to project root and temp dirs. Prevents a job from exfiltrating data to the internet or writing outside its sandbox.

**Secret redaction.** All logs/output are scanned for known secret patterns; matches are redacted with `[REDACTED]`.

**Budget caps.** Per-provider max cost/hour, per-job max tokens, per-project max cost/month. Enqueue rejects if budget exceeded.

See `docs/architecture.md` for detailed security tiers and trust model.

## Out of Scope

- **No web dashboard.** Herder is CLI + SQLite. Use `tail`, `ps`, `result` to monitor locally.
- **No multi-user.** Single-user, local SQLite. Add authentication if you need multi-user.
- **No agent-to-agent orchestration.** Jobs run independently in parallel. For workflows / DAGs, orchestrate from your frontend.
- **No remote execution.** Herder is local-only. For distributed workers, run Herder instances on each machine.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, discipline (TDD, tests pass before PR), and how to add a provider.

## License

Apache License 2.0. See [LICENSE](LICENSE).

---

**Questions?** Open an issue or check [docs/architecture.md](docs/architecture.md) for detailed design and security model.
