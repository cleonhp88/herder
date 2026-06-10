"""Stats service — aggregate metrics from jobs and attempts.

Computes read-only aggregate statistics: job counts by status, per-provider
success rates and latency percentiles, token usage, and 7-day volume trends.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from herder.db.store import Store


@dataclass
class ProviderStat:
    """Statistics for a single provider."""

    provider: str
    runs: int = 0  # Total attempts
    done: int = 0  # Successful attempts
    failed: int = 0  # failed + timeout + cancelled attempts
    success_rate: float = 0.0  # done / (done + failed)
    p50_ms: int | None = None  # 50th percentile latency
    p95_ms: int | None = None  # 95th percentile latency
    total_tokens: int = 0  # Sum of eval_count + prompt_eval_count


@dataclass
class StatsReport:
    """Complete statistics report."""

    jobs_by_status: dict[str, int] = field(default_factory=dict)
    providers: list[ProviderStat] = field(default_factory=list)
    jobs_last_7d: dict[str, int] = field(default_factory=dict)  # date -> count


def _pct(sorted_vals: list[int], q: float) -> int | None:
    """Compute a percentile from sorted values.

    Args:
        sorted_vals: Sorted list of values (e.g., durations in milliseconds).
        q: Percentile (0.0 to 1.0).

    Returns:
        Percentile value, or None if list is empty.
    """
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _tokens(usage_json: str | None) -> int:
    """Extract total tokens from JSON usage dict.

    Sums eval_count + prompt_eval_count.

    Args:
        usage_json: JSON-encoded usage dict (or None).

    Returns:
        Total tokens (0 if missing or invalid).
    """
    if not usage_json:
        return 0
    try:
        u = json.loads(usage_json)
    except (json.JSONDecodeError, TypeError):
        return 0
    return int(u.get("eval_count", 0) or 0) + int(u.get("prompt_eval_count", 0) or 0)


def compute_stats(store: Store) -> StatsReport:
    """Compute aggregate statistics from database.

    Queries jobs and attempts tables to populate:
    - jobs_by_status: count per job status
    - jobs_last_7d: jobs per day for last 7 days
    - providers: per-provider stats (success rate, latency, tokens)

    Args:
        store: SQLite store.

    Returns:
        StatsReport with all metrics.
    """
    rep = StatsReport()

    # Jobs by status
    for row in store.conn.execute(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status"
    ):
        rep.jobs_by_status[row["status"]] = row["c"]

    # Jobs per day (last 7 days, by created_at date prefix)
    for row in store.conn.execute(
        """SELECT substr(created_at,1,10) d, COUNT(*) c FROM jobs
           WHERE date(created_at) >= date('now', '-6 days')
           GROUP BY d ORDER BY d DESC LIMIT 7"""
    ):
        rep.jobs_last_7d[row["d"]] = row["c"]

    # Per-provider: join attempts → jobs.provider
    rows = store.conn.execute(
        """SELECT COALESCE(j.provider, '(none)') AS provider, a.status AS astatus,
                  a.duration_ms AS dur, a.usage AS usage
           FROM attempts a
           JOIN jobs j ON j.id = a.job_id"""
    ).fetchall()

    by_prov: dict[str, dict] = {}
    for r in rows:
        p = by_prov.setdefault(
            r["provider"],
            {"runs": 0, "done": 0, "failed": 0, "durs": [], "tokens": 0},
        )
        p["runs"] += 1
        if r["astatus"] == "done":
            p["done"] += 1
        elif r["astatus"] in ("failed", "timeout", "cancelled"):
            p["failed"] += 1
        if r["dur"] is not None:
            p["durs"].append(r["dur"])
        p["tokens"] += _tokens(r["usage"])

    for prov, d in sorted(by_prov.items()):
        durs = sorted(d["durs"])
        denom = d["done"] + d["failed"]
        rep.providers.append(
            ProviderStat(
                provider=prov,
                runs=d["runs"],
                done=d["done"],
                failed=d["failed"],
                success_rate=(d["done"] / denom) if denom else 0.0,
                p50_ms=_pct(durs, 0.50),
                p95_ms=_pct(durs, 0.95),
                total_tokens=d["tokens"],
            )
        )

    return rep
