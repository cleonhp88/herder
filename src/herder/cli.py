"""Herder command-line interface (CLI).

Thin argument parsing and command dispatch layer. Each command delegates to
a service module for implementation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from herder.adapters import default_brain_targets
from herder.config import ConfigError, format_supports, load_config
from herder.connect import auto_confirm, connect, load_recipe
from herder.init_cmd import cmd_init
from herder.resources import resolve_recipes_dir
from herder.db.store import Store, StoreError
from herder.errors import BudgetError
from herder.ids import new_job_id
from herder.loops.queue_claim import run_pending_parallel
from herder.loops.scheduler_tick import tick
from herder.services.doctor import run_doctor
from herder.services.enqueue import enqueue_job, EnqueueRequest
from herder.services.jobs import list_jobs, get_job, read_result, read_logs


# FIX 4: Set restrictive global umask (owner-only by default)
# Protects newly created files/dirs from unintended group/world access
os.umask(0o077)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Execute the 'doctor' subcommand.

    Loads config, runs provider health probes, persists results,
    and reports pass/fail based on ok provider threshold.
    Also reports security warnings for group/world-writable files.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 if passed (ok_count >= threshold), 1 if failed.
    """
    cfg = load_config(args.config)
    report = run_doctor(cfg, Store.open(), Path.cwd(), min_ok=args.min_ok,
                        config_path=args.config)

    # Print results: one line per provider (with manifest info when available)
    for h in report.rows:
        flag = "PASS" if h.noninteractive_status == "ok" else h.noninteractive_status.upper()
        base = f"{h.provider:14} {flag:12} auth={h.auth_status:8} {h.latency_ms}ms"
        prov = cfg.providers.get(h.provider)
        if prov is not None:
            supports_str = format_supports(prov.supports)
            cost_str = prov.cost_hint if prov.cost_hint is not None else "-"
            auth_env_str = prov.auth_env if prov.auth_env is not None else "on-disk"
            manifest = (
                f"  fmt={prov.output_format}"
                f" supports={supports_str}"
                f" cost={cost_str}"
                f" auth_env={auth_env_str}"
            )
        else:
            manifest = ""
        print(base + manifest)

    # Print summary
    print(f"\n{report.ok_count}/{len(report.rows)} ok (need {report.min_ok})")

    # Print any integrity warnings
    if report.warnings:
        print("\n⚠ Integrity warnings:")
        for warn in report.warnings:
            print(f"⚠ {warn}")

    return 0 if report.passed else 1


def cmd_enqueue(args: argparse.Namespace) -> int:
    """Execute the 'enqueue' subcommand.

    Validates role and project, creates a run directory, snapshots the prompt,
    and either persists the job (normal) or prints dry-run info.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 on error.
    """
    cfg = load_config(args.config)
    req = EnqueueRequest(
        project=args.project,
        role=args.role,
        kind=args.kind,
        prompt=Path(args.prompt_file).read_text(),
        priority=args.priority,
        dry_run=args.dry_run,
        runtime=args.runtime,
    )
    res = enqueue_job(cfg, Store.open(), req)

    if res.dry_run:
        print("DRY-RUN")
        print(f"provider: {res.provider}")
        print(f"argv: {res.argv}")
        print(f"cwd: {res.cwd}  (workspace_mode={res.workspace_mode})")
        print(f"permissions: {res.permissions}")
        print(f"timeout: {res.timeout}")
    else:
        print(f"enqueued {res.job_id} status={res.status}")

    return 0


def cmd_ps(args: argparse.Namespace) -> int:
    """Execute the 'ps' subcommand.

    Lists all jobs (or filtered by status/kind) in a readable table format.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success.
    """
    jobs = list_jobs(Store.open(), status=args.status, kind=args.kind)
    for j in jobs:
        print(f"{j['id']}  {j['kind']:11} {j['status']:16} {j['role'] or j['provider']}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Execute the 'inspect' subcommand.

    Prints job details as JSON.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 if job not found.
    """
    j = get_job(Store.open(), args.job_id)
    if not j:
        print("not found")
        return 1

    # Convert sqlite3.Row to dict
    job_dict = {k: j[k] for k in j.keys()}
    print(json.dumps(job_dict, indent=2, ensure_ascii=False))
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Execute the 'worker' subcommand.

    Claims and executes pending jobs. With --once, processes all currently-claimable
    jobs then exits (useful for testing and one-shot processing). Without --once,
    loops continuously, polling for new jobs at configurable interval.

    Before each processing pass, runs the scheduler tick to enqueue any jobs
    due according to cron schedules.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success.
    """
    os.umask(0o077)  # job run dirs, logs, db, result.md → owner-only (no group/other)

    cfg = load_config(args.config)
    store = Store.open()
    worker_id = args.worker_id or new_job_id().replace("job_", "w_", 1)
    lease = cfg.worker.lease_seconds

    # Register this worker in the database
    store.register_worker(worker_id)

    try:
        if args.once:
            tick(cfg, store, datetime.now(timezone.utc))
            n = run_pending_parallel(cfg, store, worker_id, lease)
            print(f"processed {n} job(s)")
            return 0

        print(f"worker {worker_id} polling every {args.interval}s (Ctrl-C to stop)")
        while True:
            store.worker_heartbeat(worker_id)
            tick(cfg, store, datetime.now(timezone.utc))
            run_pending_parallel(cfg, store, worker_id, lease)
            time.sleep(args.interval)
    finally:
        store.mark_worker_stopped(worker_id)


def cmd_result(args: argparse.Namespace) -> int:
    """Execute the 'result' subcommand.

    Prints the result.md file for a completed job.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 if job or result not found.
    """
    text = read_result(Store.open(), args.job_id)
    if text is None:
        print("not found")
        return 1
    print(text)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    """Execute the 'tail' subcommand.

    Prints stdout and stderr logs for a job.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 if job not found.
    """
    logs = read_logs(Store.open(), args.job_id, max_lines=args.lines)
    if logs is None:
        print("not found")
        return 1
    print(f"── stdout ({args.job_id}) ──")
    print(logs["stdout"])
    if logs["stderr"]:
        print("── stderr ──")
        print(logs["stderr"])
    return 0


def cmd_schedules(args: argparse.Namespace) -> int:
    """Execute the 'schedules' subcommand.

    Lists all configured schedules with their cron expressions, target projects/roles,
    and the last time they were run (if ever).

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success.
    """
    cfg = load_config(args.config)
    store = Store.open()
    if not cfg.schedules:
        print("no schedules configured")
        return 0
    for sch in cfg.schedules:
        last = store.last_scheduled_for(sch.id) or "never"
        flag = "" if sch.enabled else "  [disabled]"
        print(f"{sch.id:16} {sch.cron:16} {sch.project}/{sch.role}  last={last}{flag}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Execute the 'stats' subcommand.

    Computes and displays aggregate metrics from jobs and attempts:
    - jobs by status (counts)
    - per-provider success rate, latency (p50/p95), token usage
    - 7-day job volume trend

    Does not require config (read-only from database).

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success.
    """
    from dataclasses import asdict

    from herder.services.stats import compute_stats

    rep = compute_stats(Store.open())

    if args.json:
        print(json.dumps(asdict(rep), indent=2))
        return 0

    # Human-readable output
    print("jobs by status:")
    for st, c in sorted(rep.jobs_by_status.items()):
        print(f"  {st:16} {c}")

    print("\nper provider:")
    print(f"  {'provider':14} {'runs':>5} {'ok%':>6} {'p50ms':>7} {'p95ms':>7} {'tokens':>8}")
    for p in rep.providers:
        p50_str = str(p.p50_ms) if p.p50_ms is not None else "—"
        p95_str = str(p.p95_ms) if p.p95_ms is not None else "—"
        print(
            f"  {p.provider:14} {p.runs:>5} {p.success_rate*100:>5.0f}% "
            f"{p50_str:>7} {p95_str:>7} {p.total_tokens:>8}"
        )

    if rep.jobs_last_7d:
        print("\njobs/day (last 7 days):")
        for d in sorted(rep.jobs_last_7d.keys(), reverse=True):
            c = rep.jobs_last_7d[d]
            print(f"  {d}  {c}")

    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Execute the 'bench' subcommand.

    Reads a prompt from a file, then runs it through each specified provider
    sequentially, recording latency, token usage, and output size. Prints
    a comparison table sorted by execution time.

    Args:
        args: Parsed command-line arguments with prompt_file and providers.

    Returns:
        0 on success, 1 on error.
    """
    from herder.services.bench import run_bench

    cfg = load_config(args.config)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    provider_names = [n.strip() for n in args.providers.split(",") if n.strip()]

    rep = run_bench(cfg, prompt, provider_names)

    # Machine-readable output for programmatic consumers (e.g. the macOS monitor),
    # sorted fastest-first like the human table. Stable contract — parse this, not the table.
    if getattr(args, "json", False):
        import json as _json

        payload = {
            "prompt_chars": rep.prompt_chars,
            "results": [
                {
                    "provider": r.provider,
                    "status": r.status,
                    "duration_ms": r.duration_ms,
                    "output_len": r.output_len,
                    "tokens": r.tokens,
                    "error_type": r.error_type,
                }
                for r in sorted(rep.results, key=lambda x: x.duration_ms)
            ],
        }
        print(_json.dumps(payload))
        return 0

    # Print summary header
    print(f"bench: {rep.prompt_chars} char prompt × {len(rep.results)} providers\n")

    # Print table header
    print(f"  {'provider':14} {'status':9} {'ms':>8} {'out_len':>8} {'tokens':>8}")

    # Sort by duration and print results
    for r in sorted(rep.results, key=lambda x: x.duration_ms):
        tok = "-" if r.tokens is None else r.tokens
        print(
            f"  {r.provider:14} {r.status:9} {r.duration_ms:>8} "
            f"{r.output_len:>8} {tok:>8}"
        )

    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Execute the 'cancel' subcommand.

    Requests cancellation of a job. Does not require config.
    - pending/waiting_approval/approved → cancelled (immediate)
    - running → cancelling (worker will receive signal and finalize)
    - done/failed/cancelled → no-op

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 if job not found.
    """
    new_status = Store.open().request_cancel(args.job_id)
    if new_status is None:
        print("not found")
        return 1
    print(f"{args.job_id} → {new_status}")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """Execute the 'retry' subcommand.

    Requests retry of a job by re-queuing it.
    - failed/dead/cancelled → pending (requeued)
    - other statuses → error (cannot retry running or done jobs)

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 if job not found or cannot be retried.
    """
    store = Store.open()
    job = store.get_job(args.job_id)
    if not job:
        print("not found")
        return 1
    if job["status"] not in ("failed", "dead", "cancelled"):
        print(f"cannot retry job in status '{job['status']}'")
        return 1
    store.requeue(args.job_id)
    print(f"{args.job_id} → pending")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Execute the 'approve' subcommand.

    Approves a waiting_approval job, transitioning it to approved (claimable).
    - waiting_approval → approved
    - other statuses → no-op (reports current status as error)
    - unknown job → error

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 if job not found or cannot be approved.
    """
    new_status = Store.open().approve_job(args.job_id)
    if new_status is None:
        print("not found")
        return 1
    if new_status != "approved":
        print(f"cannot approve job in status '{new_status}'")
        return 1
    print(f"{args.job_id} → approved")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    """Execute the 'reject' subcommand.

    Rejects a waiting_approval job, transitioning it to rejected (terminal).
    - waiting_approval → rejected
    - other statuses → no-op (reports current status as error)
    - unknown job → error

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 if job not found or cannot be rejected.
    """
    new_status = Store.open().reject_job(args.job_id)
    if new_status is None:
        print("not found")
        return 1
    if new_status != "rejected":
        print(f"cannot reject job in status '{new_status}'")
        return 1
    print(f"{args.job_id} → rejected")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """Execute the 'gc' subcommand.

    Garbage collects (removes) run directories for terminal jobs older than
    the configured retention policy. Safe: never deletes non-terminal jobs
    or paths outside the runs directory.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success.
    """
    from herder.services.gc import run_gc

    cfg = load_config(args.config)
    rep = run_gc(Store.open(), cfg, datetime.now(timezone.utc), dry_run=args.dry_run)
    tag = "DRY-RUN would free" if rep.dry_run else "freed"
    mb = rep.freed_bytes / 1_048_576
    print(f"{tag} {mb:.2f} MB across {len(rep.deleted)} run dir(s)")
    print(f"kept: {rep.skipped_nonterminal} active, {rep.skipped_too_recent} within retention")
    for jid in rep.deleted:
        print(f"  - {jid}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Execute the 'add' subcommand — onboard an AI-agent hand via a recipe.

    Without an agent argument, shows an interactive menu of available recipes
    with detected status.  With an agent name, runs the full onboarding flow:
    detect → (if missing) confirm + install → login → verify → register.

    Args:
        args: Parsed command-line arguments.

    Returns:
        0 on success, 1 on error or user abort.
    """
    recipes_dir = resolve_recipes_dir(args.recipes_dir)
    config_path = Path(args.config).resolve()

    confirm_fn = auto_confirm if args.yes else _interactive_confirm_cli

    # No agent specified — show interactive menu
    if not args.agent:
        return _show_recipe_menu(recipes_dir, config_path, confirm_fn)

    recipe_path = recipes_dir / f"{args.agent}.yaml"
    try:
        recipe = load_recipe(recipe_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    brain_files = default_brain_targets(config_path)
    result = connect(
        recipe,
        config_path=config_path,
        brain_files=brain_files,
        confirm=confirm_fn,
    )

    if not result.success:
        print(f"\nAborted: {result.abort_reason}", file=sys.stderr)
        return 1

    skip_note = " (install skipped — already present)" if result.skipped_install else ""
    print(f"\nRegistered provider '{result.provider_name}' / role '{result.role_name}'{skip_note}")
    print("Updated files:")
    for p in result.files_updated:
        print(f"  {p}")
    return 0


def _interactive_confirm_cli(prompt: str) -> bool:
    """Interactive y/N prompt for CLI use.

    Args:
        prompt: Question shown to user.

    Returns:
        True only when user types 'y' or 'Y'.
    """
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"


def _show_recipe_menu(
    recipes_dir: Path,
    config_path: Path,
    confirm_fn: object,
) -> int:
    """Show a menu of available recipes with detected status.

    Args:
        recipes_dir: Directory containing recipe YAML files.
        config_path: Path to config.yaml (unused here, for future use).
        confirm_fn: Confirm callable (unused here, menu is read-only).

    Returns:
        0 always.
    """
    import subprocess as _sp

    if not recipes_dir.exists():
        print("no recipes directory found")
        return 0

    recipe_files = sorted(recipes_dir.glob("*.yaml"))
    if not recipe_files:
        print("no recipes found")
        return 0

    print("Available agents (run 'herder add <agent>' to install):\n")
    for recipe_file in recipe_files:
        try:
            recipe = load_recipe(recipe_file)
        except (FileNotFoundError, ValueError):
            continue
        status_result = _sp.run(
            recipe.detect, shell=True, capture_output=True
        )
        status = "installed" if status_result.returncode == 0 else "not installed"
        icon = "✓" if status_result.returncode == 0 else "✗"
        print(f"  {icon} {recipe.name:16} {status}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(prog="herder")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")

    sub = parser.add_subparsers(dest="cmd", required=True, help="Subcommand to run")

    # doctor subcommand
    d = sub.add_parser("doctor", help="Probe provider health")
    d.add_argument(
        "--min-ok",
        type=int,
        default=None,
        dest="min_ok",
        help="Override minimum ok providers threshold",
    )
    d.set_defaults(func=cmd_doctor)

    # enqueue subcommand
    e = sub.add_parser("enqueue", help="Enqueue a job")
    e.add_argument("--project", required=True, help="Project name")
    e.add_argument("--role", required=True, help="Role name")
    e.add_argument("--kind", required=True, help="Job kind")
    e.add_argument("--prompt-file", required=True, dest="prompt_file", help="Path to prompt file")
    e.add_argument("--priority", type=int, default=0, help="Job priority")
    e.add_argument("--dry-run", action="store_true", dest="dry_run", help="Dry-run (don't persist)")
    e.add_argument("--runtime", default=None, help="Runtime name (default: resolve via fallback)")
    e.set_defaults(func=cmd_enqueue)

    # ps subcommand
    p = sub.add_parser("ps", help="List jobs")
    p.add_argument("--status", default=None, help="Filter by status")
    p.add_argument("--kind", default=None, help="Filter by kind")
    p.set_defaults(func=cmd_ps)

    # inspect subcommand
    i = sub.add_parser("inspect", help="Inspect a job")
    i.add_argument("job_id", help="Job ID to inspect")
    i.set_defaults(func=cmd_inspect)

    # worker subcommand
    w = sub.add_parser("worker", help="Process pending jobs")
    w.add_argument(
        "--once",
        action="store_true",
        help="Process all claimable jobs then exit (default: continuous polling loop)",
    )
    w.add_argument(
        "--worker-id",
        dest="worker_id",
        default=None,
        help="Worker ID (auto-generated if not provided)",
    )
    w.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Poll interval in seconds (only used in continuous mode, default 5)",
    )
    w.set_defaults(func=cmd_worker)

    # result subcommand
    r = sub.add_parser("result", help="Print result.md for a job")
    r.add_argument("job_id", help="Job ID to retrieve result for")
    r.set_defaults(func=cmd_result)

    # tail subcommand
    t = sub.add_parser("tail", help="Print stdout/stderr logs for a job")
    t.add_argument("job_id", help="Job ID to retrieve logs for")
    t.add_argument("--lines", type=int, default=200, help="Max lines per log (default 200)")
    t.set_defaults(func=cmd_tail)

    # cancel subcommand
    c = sub.add_parser("cancel", help="Request cancellation of a job")
    c.add_argument("job_id", help="Job ID to cancel")
    c.set_defaults(func=cmd_cancel)

    # retry subcommand
    r = sub.add_parser("retry", help="Retry a failed or dead job")
    r.add_argument("job_id", help="Job ID to retry")
    r.set_defaults(func=cmd_retry)

    # approve subcommand
    ap = sub.add_parser("approve", help="Approve a waiting_approval job")
    ap.add_argument("job_id", help="Job ID to approve")
    ap.set_defaults(func=cmd_approve)

    # reject subcommand
    rj = sub.add_parser("reject", help="Reject a waiting_approval job")
    rj.add_argument("job_id", help="Job ID to reject")
    rj.set_defaults(func=cmd_reject)

    # schedules subcommand
    sc = sub.add_parser("schedules", help="List configured schedules")
    sc.set_defaults(func=cmd_schedules)

    # stats subcommand
    st = sub.add_parser("stats", help="Show aggregate metrics and statistics")
    st.add_argument("--json", action="store_true", help="Output as JSON")
    st.set_defaults(func=cmd_stats)

    # gc subcommand
    g = sub.add_parser("gc", help="Garbage collect old run directories")
    g.add_argument("--dry-run", action="store_true", dest="dry_run", help="Preview deletions without deleting")
    g.set_defaults(func=cmd_gc)

    # add subcommand
    a = sub.add_parser("add", help="Onboard an AI-agent hand via a recipe")
    a.add_argument(
        "agent",
        nargs="?",
        default=None,
        help="Agent name (e.g. 'kiro'). Omit to show interactive menu.",
    )
    a.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Non-interactive: auto-confirm all prompts (for scripting/tests).",
    )
    a.add_argument(
        "--recipes-dir",
        dest="recipes_dir",
        default=None,
        help="Directory containing recipe YAML files (default: ./recipes/ or bundled)",
    )
    a.set_defaults(func=cmd_add)

    # init subcommand
    ini = sub.add_parser("init", help="First-run setup wizard (config + brain wiring + doctor)")
    ini.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Non-interactive: skip all prompts, do not launch add wizard.",
    )
    ini.add_argument(
        "--recipes-dir",
        dest="recipes_dir",
        default=None,
        help="Directory containing recipe YAML files (default: ./recipes/ or bundled)",
    )
    ini.set_defaults(func=cmd_init)

    # bench subcommand
    b = sub.add_parser("bench", help="Benchmark providers on a prompt")
    b.add_argument("--prompt-file", required=True, dest="prompt_file", help="Path to prompt file")
    b.add_argument(
        "--providers",
        required=True,
        help="Comma-separated provider names to benchmark"
    )
    b.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report instead of the human table"
    )
    b.set_defaults(func=cmd_bench)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the appropriate command.

    Wraps command execution with error handling for ConfigError and StoreError.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code from the executed command (0 on success, 1 on error).
    """
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, StoreError, BudgetError, OSError, UnicodeDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
