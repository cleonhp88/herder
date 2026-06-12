# ADR 0002 — Role fallback list + per-backend cooldown (Tier 2)

- **Status:** Accepted
- **Date:** 2026-06-12
- **Builds on:** [ADR 0001](0001-agent-hub-strategy.md) (generic-CLI core), roadmap Tier 2

## Context

A role bound to a single provider stalls when that backend is rate-limited,
hanging, or mis-configured — even when an equivalent backend could serve the
job. Tier 2 adds an ordered fallback list per role and a cooldown signal so
routing avoids a sick backend. Patterns follow LiteLLM (`fallbacks`, cooldown)
and OpenRouter (ordered model fallback).

## Decisions

1. **Fallback binds at enqueue, advances on requeue** (not claim-time late
   binding). `jobs.provider` is set at enqueue to the first non-cooling
   provider; on a retryable failure the requeue UPDATE atomically advances it
   to the next provider in the role's list (wrap-around, failed provider tried
   last). Claim SQL, parallel per-provider semaphores, and dry-run stay
   untouched. The per-attempt provider is recorded in `attempts.provider`
   (schema v3), so the audit trail survives the mutation of `jobs.provider`.

2. **Config shape:** `Role.providers: list[str]` is canonical; legacy
   `provider: str` is normalized to a one-element list by a before-validator
   (both keys together is an error). All providers in the list must exist and
   pass the Tier 1 capability check at load time — fail loud, no runtime skip.

3. **Cooldown is a routing hint, not a gate.** A provider is "cooling" when it
   has ≥ `allowed_fails` (default 3) failed **or timed-out** attempts within
   `window_seconds` (default 300). Selection skips cooling candidates; when ALL
   candidates are cooling it returns the primary and logs a WARNING — a job is
   never stranded. Timeouts count because a hanging backend is the most common
   sickness signal; counting only loud failures would keep routing to a hung
   provider forever.

4. **Failure counts are global per provider, thresholds per role.** A backend
   broken for one role is broken for everyone; how tolerant a role is of that
   signal (`cooldown:` block) is per-role. Single-provider roles get no
   cooldown protection by construction.

5. **`max_retries` stays per-job** (total attempts across all providers). Want
   3 tries per provider on a 3-provider role → set `max_retries: 9`. No
   per-provider retry accounting.

6. **State lives in SQLite, never in memory.** Cooldown is computed from
   `attempts` (`provider`, `status`, `finished_at`) via one covering-index
   query, so `--once` workers, parallel threads, and future daemons agree.

## The datetime trap (do not regress)

`attempts.finished_at` stores Python `datetime.isoformat()` strings
(`2026-06-12T03:24:02+00:00`). SQLite's `datetime('now')` renders
`2026-06-12 03:24:02`. Comparing the two lexicographically is silently wrong
(`'T' > ' '` makes same-day rows always pass a window check). The cooldown
threshold is therefore **computed in Python** with
`(now(utc) - timedelta(seconds=window)).isoformat()` and compared
string-to-string in the same format family.
`test_count_recent_failures_respects_window` is the canary.

## Consequences

- A role listing N providers survives N-1 backend outages; new enqueues route
  around a backend after `allowed_fails` failures/timeouts within the window.
- One more SQL query per candidate at routing time (covering index,
  `EXPLAIN QUERY PLAN` confirms zero table reads). N ≤ ~5 in practice.
- `jobs.provider` is no longer immutable across a job's life; consumers must
  use `attempts.provider` for per-attempt attribution (pre-v3 rows are NULL
  and never count toward cooldown).
- Legacy configs (`provider: str`) load unchanged.

## Alternatives rejected

| Alternative | Why rejected |
|---|---|
| Claim-time late binding (job stores role only) | Rewrites claim SQL + semaphore keying + dry-run for no audit benefit; attempts.provider already preserves attribution |
| Cooldown via in-memory counters | Dies with the process; wrong for `--once` and multi-process workers |
| Cooldown as hard gate (refuse when all cooling) | Strands jobs; retry/dead-letter already bounds spend |
| Per-provider max_retries | Combinatorial accounting for marginal value; `max_retries` scaling is explicit and understandable |
| SQLite `datetime('now')` threshold | Format mismatch with isoformat storage — silently broken window (see trap above) |
