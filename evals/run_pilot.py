"""Pilot eval runner — enqueue, execute, score, report.

Usage::

    python evals/run_pilot.py --config config.yaml \\
        --cases evals/cases/pilot.yaml \\
        --providers ollama_lan,claude_cli,codex_cli \\
        [--role-map ollama_lan=local,codex_cli=coder] \\
        [--run-id pilot-YYYYMMDD-HHMM]

Each case × provider × rep is enqueued with a deterministic idempotency key
``{run_id}-{case_id}-{provider}-r{rep}``.  Re-running with the same run-id
resumes (idempotent); a new run-id starts fresh.

Provider → role resolution: by default each provider maps 1:1 to a role of the
same name (the recommended config convention).  Use ``--role-map`` to override
individual providers when your config names roles differently.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import textwrap
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Ensure the project src and project root are importable when run as a script
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
for _p in [str(_PROJECT_ROOT / "src"), str(_PROJECT_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from herder.config import load_config  # noqa: E402
from herder.db.store import Store  # noqa: E402
from herder.loops.queue_claim import run_pending_once  # noqa: E402
from herder.paths import home as _herder_home  # noqa: E402
from herder.services.enqueue import EnqueueRequest, enqueue_job  # noqa: E402

from evals.scorer import score  # noqa: E402

# ---------------------------------------------------------------------------
# Single-flight lock — prevents concurrent run_pilot processes
# ---------------------------------------------------------------------------


class EvalLockError(RuntimeError):
    """Raised when another live run_pilot already holds the eval lock.

    Args:
        lock_path: Path to the lock file.
        holder_pid: PID read from the lock file, or None if unreadable.
    """

    def __init__(self, lock_path: Path, holder_pid: int | None) -> None:
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        who = f"pid {holder_pid}" if holder_pid else "another process"
        super().__init__(
            f"another run_pilot is already running ({who} holds {lock_path}); "
            "only one eval run may execute at a time"
        )


@contextlib.contextmanager
def _single_flight_lock() -> Iterator[None]:
    """Exclusive run-level lock using ``fcntl.flock`` (macOS / Linux).

    Lock file is ``herder.paths.home() / "eval.lock"``.  The file is
    persistent (never unlinked) so that the kernel flock is tied to a stable
    inode; unlinking would introduce a fresh-inode race between a dying holder
    and a new contender.

    On acquisition, the current PID is written into the file for diagnostics
    only — flock-failure is the authoritative signal, never the file content.

    The kernel releases the flock automatically on process death (close or
    signal), so no stale-lock reclaim is needed.

    Raises:
        EvalLockError: If another live process holds the lock.
    """
    lock_path = _herder_home() / "eval.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # Another live process holds the lock — read its pid best-effort.
        holder_pid: int | None = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, 32).decode(errors="replace").strip()
            holder_pid = int(content) if content else None
        except (OSError, ValueError):
            pass
        os.close(fd)
        raise EvalLockError(lock_path, holder_pid)

    # Acquired — write our pid for diagnostics (races nothing; we hold LOCK_EX).
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass  # Best-effort diagnostic write; lock is still held.

    try:
        yield
    finally:
        # Best-effort release; kernel also releases on fd close / process death.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        # NOTE: lock file is intentionally NOT unlinked — persistent inode
        # prevents the flock-vs-unlink race.


# ---------------------------------------------------------------------------
# Provider → role mapping
# ---------------------------------------------------------------------------

# Default convention: a provider maps 1:1 to a role of the same name. Supply
# ``--role-map prov=role,...`` to override individual providers whose config
# role differs from the provider name. See ``_parse_role_map``.

# Hard cap on run_pending_once iterations per phase-EXECUTE loop to prevent
# infinite loops when workers stall.
_MAX_EXECUTE_ITERATIONS = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_role_map(spec: str | None) -> dict[str, str]:
    """Parse a ``prov1=role1,prov2=role2`` string into a dict.

    An empty or None spec yields an empty map (every provider then falls back
    to role=provider at the resolve site).

    Args:
        spec: Comma-separated ``provider=role`` pairs, or None.

    Returns:
        Mapping provider name → role name.

    Raises:
        ValueError: If a pair is malformed (missing '=', empty side).
    """
    role_map: dict[str, str] = {}
    if not spec:
        return role_map
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(
                f"bad --role-map entry {pair!r}: expected 'provider=role'"
            )
        prov, _, role = pair.partition("=")
        prov, role = prov.strip(), role.strip()
        if not prov or not role:
            raise ValueError(
                f"bad --role-map entry {pair!r}: provider and role must be non-empty"
            )
        role_map[prov] = role
    return role_map


def _default_run_id() -> str:
    """Generate default run-id from current UTC time."""
    now = datetime.now(timezone.utc)
    return now.strftime("pilot-%Y%m%d-%H%M")


def _strip_frontmatter(text: str) -> str:
    """Strip YAML frontmatter between first two '---' lines.

    Args:
        text: Raw file content (may include frontmatter).

    Returns:
        Body text after frontmatter, or original text if no frontmatter found.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    # Find second '---'
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body = "".join(lines[i + 1 :])
            return body.lstrip("\n")
    return text


def _load_cases(cases_path: str) -> dict[str, Any]:
    """Load and validate the cases YAML.

    Args:
        cases_path: Path to the YAML cases file.

    Returns:
        Parsed YAML document as a dict.

    Raises:
        SystemExit: If file is missing or malformed.
    """
    try:
        with open(cases_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"[ERROR] cases file not found: {cases_path}", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"[ERROR] bad YAML in {cases_path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict) or "cases" not in data:
        print("[ERROR] cases file must have a top-level 'cases' key", file=sys.stderr)
        sys.exit(1)
    return data


def _get_last_attempt(store: Store, job_id: str) -> Any | None:
    """Return the last attempt row for a job, or None.

    Args:
        store: Open Store instance.
        job_id: Job identifier.

    Returns:
        sqlite3.Row of the last attempt, or None.
    """
    attempts = store.attempts_for_job(job_id)
    if not attempts:
        return None
    return attempts[-1]


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def phase_enqueue(
    cfg: Any,
    store: Store,
    cases: list[dict],
    providers: list[str],
    reps: int,
    kind: str,
    run_id: str,
    role_map: dict[str, str],
) -> dict[str, dict]:
    """Phase ENQUEUE: enqueue one job per (case × provider × rep).

    Uses idempotency keys so re-running with the same run-id resumes.

    Args:
        cfg: Loaded Config object.
        store: Open Store instance.
        cases: List of case dicts from the YAML.
        providers: Provider names to test.
        reps: Number of repetitions per (case, provider).
        kind: Job kind for all enqueued jobs.
        run_id: Unique run identifier.
        role_map: Provider → role overrides; absent providers default to
            role=provider.

    Returns:
        Mapping job_id → {case_id, provider, rep} for all enqueued/resumed jobs.
    """
    job_map: dict[str, dict] = {}  # job_id → metadata

    for case in cases:
        case_id = case["id"]
        prompt = case["prompt"]

        for provider in providers:
            role = role_map.get(provider, provider)

            for rep in range(1, reps + 1):
                ikey = f"{run_id}-{case_id}-{provider}-r{rep}"
                req = EnqueueRequest(
                    project="scratch",
                    role=role,
                    kind=kind,
                    prompt=prompt,
                    idempotency_key=ikey,
                )
                try:
                    result = enqueue_job(cfg, store, req)
                    if result.job_id:
                        job_map[result.job_id] = {
                            "case_id": case_id,
                            "provider": provider,
                            "rep": rep,
                            "ikey": ikey,
                        }
                        print(
                            f"  [ENQUEUE] {ikey} → job {result.job_id}"
                            f" (status={result.status})"
                        )
                except Exception as e:  # noqa: BLE001
                    print(
                        f"  [ENQUEUE ERROR] {ikey}: {e}", file=sys.stderr
                    )

    return job_map


def phase_execute(cfg: Any, store: Store, worker_id: str) -> None:
    """Phase EXECUTE: drain the pending queue.

    Loops run_pending_once until it returns 0 or the hard cap is hit.

    Args:
        cfg: Loaded Config object.
        store: Open Store instance.
        worker_id: Worker identifier string.
    """
    print("\n[EXECUTE] draining pending jobs …")
    total = 0
    for i in range(_MAX_EXECUTE_ITERATIONS):
        n = run_pending_once(cfg, store, worker_id, lease_seconds=300)
        total += n
        if n == 0:
            break
        print(f"  iteration {i + 1}: processed {n} job(s) (total={total})")
    print(f"  done — {total} job(s) processed")


def phase_score(
    store: Store,
    cases: list[dict],
    job_map: dict[str, dict],
) -> list[dict]:
    """Phase SCORE: read output, strip frontmatter, run scorer.

    Provider failure is treated as DATA (eval continues); eval never crashes.

    Args:
        store: Open Store instance.
        cases: List of case dicts for assertion lookup.
        job_map: Mapping job_id → {case_id, provider, rep}.

    Returns:
        List of raw result records (one per job).
    """
    # Build case_id → assertions index
    assertions_by_id: dict[str, list[dict]] = {c["id"]: c["assertions"] for c in cases}

    records: list[dict] = []

    for job_id, meta in job_map.items():
        case_id = meta["case_id"]
        provider = meta["provider"]
        rep = meta["rep"]
        assertions = assertions_by_id.get(case_id, [])

        job = store.get_job(job_id)
        if job is None:
            records.append(
                {
                    "case_id": case_id,
                    "provider": provider,
                    "rep": rep,
                    "job_id": job_id,
                    "status": "missing",
                    "passed": False,
                    "failures": ["NO_OUTPUT"],
                    "notes": ["job not found in store"],
                    "duration_ms": None,
                    "usage": None,
                    "body_excerpt": "",
                }
            )
            continue

        job_status = job["status"]
        attempt = _get_last_attempt(store, job_id)
        duration_ms = attempt["duration_ms"] if attempt else None
        usage_raw = attempt["usage"] if attempt else None
        usage: dict | None = None
        if usage_raw:
            try:
                usage = json.loads(usage_raw) if isinstance(usage_raw, str) else usage_raw
            except (json.JSONDecodeError, TypeError):
                usage = None

        if job_status != "done":
            records.append(
                {
                    "case_id": case_id,
                    "provider": provider,
                    "rep": rep,
                    "job_id": job_id,
                    "status": job_status,
                    "passed": False,
                    "failures": [f"ERROR:{job_status}"],
                    "notes": [
                        f"job ended with non-done status: {job_status}"
                        + (
                            f" (error_type={job['error_type']})"
                            if job["error_type"]
                            else ""
                        )
                    ],
                    "duration_ms": duration_ms,
                    "usage": usage,
                    "body_excerpt": "",
                }
            )
            continue

        # Read output file
        output_path = job["output_path"]
        body = ""
        if output_path:
            try:
                raw = Path(output_path).read_text(encoding="utf-8")
                body = _strip_frontmatter(raw)
            except OSError as e:
                records.append(
                    {
                        "case_id": case_id,
                        "provider": provider,
                        "rep": rep,
                        "job_id": job_id,
                        "status": "done",
                        "passed": False,
                        "failures": ["NO_OUTPUT"],
                        "notes": [f"could not read output_path: {e}"],
                        "duration_ms": duration_ms,
                        "usage": usage,
                        "body_excerpt": "",
                    }
                )
                continue
        else:
            records.append(
                {
                    "case_id": case_id,
                    "provider": provider,
                    "rep": rep,
                    "job_id": job_id,
                    "status": "done",
                    "passed": False,
                    "failures": ["NO_OUTPUT"],
                    "notes": ["output_path is None"],
                    "duration_ms": duration_ms,
                    "usage": usage,
                    "body_excerpt": "",
                }
            )
            continue

        result = score(body, assertions)
        records.append(
            {
                "case_id": case_id,
                "provider": provider,
                "rep": rep,
                "job_id": job_id,
                "status": job_status,
                "passed": result.passed,
                "failures": result.failures,
                "notes": result.notes,
                "duration_ms": duration_ms,
                "usage": usage,
                "body_excerpt": body[:500],
            }
        )

    return records


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _build_matrix(
    records: list[dict],
    cases: list[dict],
    providers: list[str],
    reps: int,
) -> str:
    """Build markdown pass-count matrix table.

    Cells are ``{pass_count}/{total_reps}`` or ``E`` for all-error reps.

    Args:
        records: Scored result records.
        cases: Case list (for row ordering / IDs).
        providers: Provider list (for column ordering).
        reps: Number of reps per (case, provider).

    Returns:
        Markdown table string.
    """
    # Aggregate: (case_id, provider) → list[passed|error]
    agg: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in records:
        key = (r["case_id"], r["provider"])
        if r["status"] != "done" and not r["passed"]:
            agg[key].append("E")
        else:
            agg[key].append("P" if r["passed"] else "F")

    case_ids = [c["id"] for c in cases]

    header = "| case | " + " | ".join(providers) + " |"
    sep = "|------|" + "|".join(["------"] * len(providers)) + "|"
    rows = [header, sep]

    for cid in case_ids:
        cells = []
        for prov in providers:
            results = agg.get((cid, prov), [])
            if not results:
                cells.append("-")
            elif all(v == "E" for v in results):
                cells.append("E")
            else:
                pass_count = sum(1 for v in results if v == "P")
                cells.append(f"{pass_count}/{len(results)}")
        rows.append("| " + cid + " | " + " | ".join(cells) + " |")

    return "\n".join(rows)


def _taxonomy_counts(
    records: list[dict],
    providers: list[str],
) -> str:
    """Build per-provider taxonomy failure count section.

    Args:
        records: Scored result records.
        providers: Provider list.

    Returns:
        Markdown text.
    """
    lines = []
    for prov in providers:
        counts: dict[str, int] = defaultdict(int)
        for r in records:
            if r["provider"] == prov:
                for f in r["failures"]:
                    counts[f] += 1
        if counts:
            items = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
            lines.append(f"- **{prov}**: {items}")
        else:
            lines.append(f"- **{prov}**: (no failures)")
    return "\n".join(lines)


def _median_durations(
    records: list[dict],
    providers: list[str],
) -> str:
    """Build per-provider median duration section.

    Args:
        records: Scored result records.
        providers: Provider list.

    Returns:
        Markdown text.
    """
    lines = []
    for prov in providers:
        durations = [
            r["duration_ms"]
            for r in records
            if r["provider"] == prov and r["duration_ms"] is not None
        ]
        if durations:
            durations_sorted = sorted(durations)
            n = len(durations_sorted)
            mid = n // 2
            median = (
                durations_sorted[mid]
                if n % 2 == 1
                else (durations_sorted[mid - 1] + durations_sorted[mid]) / 2
            )
            lines.append(f"- **{prov}**: {median:.0f} ms (n={n})")
        else:
            lines.append(f"- **{prov}**: no timing data")
    return "\n".join(lines)


def _honest_notes(records: list[dict]) -> str:
    """List every failure/error with taxonomy + 200-char body excerpt.

    Args:
        records: Scored result records.

    Returns:
        Markdown text.
    """
    lines = []
    for r in records:
        if r["passed"]:
            continue
        label = f"{r['case_id']} / {r['provider']} / rep{r['rep']}"
        failures_str = ", ".join(r["failures"]) if r["failures"] else "(none recorded)"
        excerpt = r.get("body_excerpt", "")[:200].replace("\n", " ↵ ")
        lines.append(f"- **{label}**: `{failures_str}`")
        if excerpt:
            lines.append(f"  > {excerpt!r}")
    return "\n".join(lines) if lines else "_All jobs passed._"


def _build_conditions_section(
    cfg: Any | None,
    providers: list[str],
    reps: int,
) -> str:
    """Build the ## Conditions section for the Markdown report.

    Lists per-provider timeouts, sandbox confound, and non-determinism caveat.

    Args:
        cfg: Loaded Config object (may be None in tests / offline use).
        providers: Provider names used in the run.
        reps: Number of repetitions per (case, provider).

    Returns:
        Markdown text for the conditions section.
    """
    lines: list[str] = []

    # (a) Per-provider timeouts — asymmetric, listed explicitly
    if cfg is not None and hasattr(cfg, "providers"):
        timeout_items = []
        for p in providers:
            prov_obj = cfg.providers.get(p)
            if prov_obj is not None:
                timeout_items.append(f"  - **{p}**: {prov_obj.timeout}s")
            else:
                timeout_items.append(f"  - **{p}**: (unknown — not in config)")
        timeouts_str = "\n".join(timeout_items) if timeout_items else "  - (no provider config available)"
    else:
        timeouts_str = "  - (config not available)"
    lines.append("**a) Per-provider timeouts (asymmetric — compare with caution):**")
    lines.append(timeouts_str)
    lines.append("")

    # (b) Sandbox confound
    lines.append(
        "**b) Sandbox confound:** `read_only` roles (`local`, `planner`) run under "
        "seatbelt sandbox; `coder` role runs **unsandboxed** (`allow_tools=True`). "
        "Pass-rates are not directly comparable across these tiers."
    )
    lines.append("")

    # (c) Non-determinism caveat
    lines.append(
        f"**c) Non-deterministic — no seed control:** {reps} rep(s) per "
        "(case × provider). Rankings are a **directional smoke-test only**, "
        "not a controlled benchmark."
    )

    return "\n".join(lines)


def phase_report(
    records: list[dict],
    cases: list[dict],
    providers: list[str],
    reps: int,
    run_id: str,
    outputs_dir: Path,
    cfg: Any | None = None,
) -> None:
    """Phase REPORT: write .md and .jsonl output files.

    Args:
        records: Scored result records.
        cases: Case list.
        providers: Provider list.
        reps: Number of reps per (case, provider).
        run_id: Unique run identifier.
        outputs_dir: Directory for output files.
        cfg: Loaded Config object (optional).  When supplied, per-provider
            timeouts are shown in the conditions section.
    """
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # JSONL
    jsonl_path = outputs_dir / f"{run_id}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"\n[REPORT] wrote {jsonl_path}")

    # Markdown
    matrix = _build_matrix(records, cases, providers, reps)
    taxonomy = _taxonomy_counts(records, providers)
    medians = _median_durations(records, providers)
    notes_section = _honest_notes(records)
    conditions = _build_conditions_section(cfg, providers, reps)

    total = len(records)
    passed = sum(1 for r in records if r["passed"])

    md = textwrap.dedent(
        f"""\
        # Pilot eval — {run_id}

        ## Conditions (đọc trước khi so sánh)

        {conditions}

        **Total jobs:** {total} | **Passed:** {passed} | **Failed/Error:** {total - passed}

        ## Pass matrix

        {matrix}

        ## Error taxonomy counts (per provider)

        {taxonomy}

        ## Median duration (per provider)

        {medians}

        ## Honest failure notes

        {notes_section}
        """
    )

    md_path = outputs_dir / f"{run_id}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"[REPORT] wrote {md_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    p = argparse.ArgumentParser(
        prog="run_pilot.py",
        description="herder eval pilot runner — enqueue, execute, score, report.",
    )
    p.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to herder config.yaml.",
    )
    p.add_argument(
        "--cases",
        required=True,
        metavar="PATH",
        help="Path to eval cases YAML (e.g. evals/cases/pilot.yaml).",
    )
    p.add_argument(
        "--providers",
        required=True,
        metavar="P1,P2,...",
        help=(
            "Comma-separated provider names to test. Each provider maps 1:1 to "
            "a role of the same name unless overridden via --role-map."
        ),
    )
    p.add_argument(
        "--role-map",
        default=None,
        metavar="P1=R1,P2=R2,...",
        dest="role_map",
        help=(
            "Optional provider→role overrides as comma-separated pairs. "
            "Providers not listed default to role=provider."
        ),
    )
    p.add_argument(
        "--run-id",
        default=None,
        metavar="RUN_ID",
        dest="run_id",
        help="Unique run identifier (default: pilot-YYYYMMDD-HHMM). "
        "Using the same run-id resumes; a new run-id starts fresh.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the pilot eval pipeline.

    Args:
        argv: CLI argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 = success).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    run_id = args.run_id or _default_run_id()
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

    if not providers:
        print("[ERROR] --providers must be a non-empty comma-separated list", file=sys.stderr)
        return 1

    try:
        role_map = _parse_role_map(args.role_map)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    print(f"=== Pilot eval  run_id={run_id} ===")
    print(f"    config   : {args.config}")
    print(f"    cases    : {args.cases}")
    print(f"    providers: {providers}")
    print(f"    role map : {role_map or '(1:1 provider→role)'}")

    # Load config and open store
    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] failed to load config: {e}", file=sys.stderr)
        return 1

    store = Store.open()

    # Load cases YAML
    cases_doc = _load_cases(args.cases)
    defaults = cases_doc.get("defaults", {})
    reps: int = int(defaults.get("reps", 2))
    kind: str = str(defaults.get("kind", "eval"))
    cases: list[dict] = cases_doc["cases"]

    print(f"    cases    : {len(cases)} | reps={reps} | kind={kind}")

    # ── Single-flight lock — prevent concurrent run_pilot processes ────────
    try:
        with _single_flight_lock():
            # ── Phase ENQUEUE ──────────────────────────────────────────────
            print(f"\n[ENQUEUE] {len(cases)} cases × {len(providers)} providers × {reps} reps …")
            worker_id = f"eval-runner-{run_id}"
            job_map = phase_enqueue(
                cfg, store, cases, providers, reps, kind, run_id, role_map
            )
            print(f"  {len(job_map)} job(s) tracked")

            # ── Phase EXECUTE ──────────────────────────────────────────────
            phase_execute(cfg, store, worker_id)

            # ── Phase SCORE ────────────────────────────────────────────────
            print("\n[SCORE] scoring outputs …")
            records = phase_score(store, cases, job_map)
            passed = sum(1 for r in records if r["passed"])
            print(f"  {passed}/{len(records)} passed")

            # ── Phase REPORT ───────────────────────────────────────────────
            outputs_dir = _HERE / "outputs"
            phase_report(records, cases, providers, reps, run_id, outputs_dir, cfg=cfg)

            return 0
    except EvalLockError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
