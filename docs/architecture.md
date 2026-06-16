# Herder Architecture

## Supervisor Model

```
Frontend (Claude Code / CLI)
    |
    v
[Herder CLI]  enqueue() -> SQLite queue
    ^
    |
    v
[SQLite Database] <- jobs, attempts, schedules, health, workers
    ^
    |
    v
[Worker Daemon]  claims job -> spawns provider subprocess -> writes result.md
    |
    v
[Providers]  CLI adapters (claude, codex, gemini, ollama)
    |
    v
[External AI APIs / Ollama HTTP]
```

The supervisor is stateless: all state lives in SQLite. The worker daemon polls the queue, atomically claims a job with a lease, spawns a provider subprocess, captures output, and writes `result.md`. The result can be fetched via `herder result <job_id>`.

## SQLite Schema

| Table | Purpose |
|-------|---------|
| `jobs` | Job queue: id, project, role, prompt, status (pending/claimed/completed/failed), created/updated timestamps, approval flags. |
| `attempts` | Job execution history: job_id, provider, exit_code, duration, tokens_in/out, cost, error_msg. |
| `schedules` | Cron jobs: id, cron expr, project, role, prompt_file, enabled flag. |
| `schedule_runs` | Cron execution records: schedule_id, run_timestamp, job_id. |
| `provider_health` | Provider status: provider_name, last_check, status (ok/fail/timeout), error_msg, latency_ms. |
| `workers` | Active worker instances: worker_id, heartbeat_timestamp, claimed_job_id, claimed_at. |

## Provider Adapter Contract

A provider is a subprocess that reads a prompt from stdin (or arg/env) and writes output to stdout. Herder wraps it with:
- Timeout (configurable, default 1800s)
- Cost tracking (tokens × price per provider)
- Output parsing (text, JSON, codex_exec_json)
- Env minimization (allow_env whitelist)
- Secret redaction

### How to Add a Provider

1. **Add a config block** in `config.yaml`:
   ```yaml
   providers:
     my_cli:
       type: cli
       executable: my-ai-cli
       args: ["-p"]           # prepended to prompt
       input: stdin           # or arg_or_stdin, env
       timeout: 1200
       max_concurrency: 2
       parser: text           # or json, codex_exec_json
       env_profile: my_cli    # optional, references env_profiles section
   ```

2. **Define env_profile** (optional, if secrets needed):
   ```yaml
   env_profiles:
     my_cli:
       allow_env: [MY_API_KEY, MY_AUTH_TOKEN]
   ```

3. **Add to a role**:
   ```yaml
   roles:
     my_role:
       provider: my_cli
       permissions: read_only
       output_format: report
       retry_policy: standard
   ```

4. **Test** with `herder doctor`:
   ```bash
   herder --config config.yaml doctor
   ```

For custom output parsing or advanced features (streaming, batch mode), write a small adapter module in `herder/providers/` and register it in the provider registry.

## Security Tiers

### Tier 1: Trusted (Native CLI + Environment Minimization)

- Read-only or worktree_write permissions
- Native CLI subprocess (no sandbox overhead)
- env_profiles whitelist controls which secrets are exposed
- Suitable for official Claude/Codex/Gemini CLIs on your machine

**Guarantees:**
- Only allow_env variables visible to subprocess
- Output scrubbed for secret patterns

### Tier 2: Untrusted Preset (Seatbelt Sandbox — macOS only)

- Force seatbelt profile on subprocess: deny network, confine writes to project root + temp
- Blocks `socket()` system calls (prevents exfiltration)
- Cannot write outside sandbox (prevents rootkit/backdoor)
- Suitable for 3rd-party or untrusted CLIs

**Guarantees:**
- No outbound network access
- No writes outside project root / temp
- All logs redacted

**Note:** Tier 2 is currently macOS-only (seatbelt is macOS feature). Linux support is experimental.

### Tier 3: VM Sandbox (Future)

- Run provider in ephemeral VM (e.g., UTM on macOS)
- Maximal isolation; provider has no access to host filesystem or network
- Highest latency and cost

### Blocking Untrusted Code

Jobs referencing `secret_keys: [api_key_name]` fail unless:
- The worker was started with `--unlock-secret api_key_name`, OR
- The job was explicitly approved (requires_approval=true)

This prevents a malicious config from silently leaking your API keys.

## Out of Scope

- **Multi-user.** Herder assumes single-user local access. Add authentication if needed.
- **Agent orchestration / workflows.** Jobs run independently. Orchestrate from your frontend.
- **Remote execution.** Herder is local-only. Run separate instances on each machine.
- **Web dashboard.** Use CLI commands (`ps`, `tail`, `result`) to monitor.

---

See [../README.md](../README.md) for user-facing docs and quickstart.
