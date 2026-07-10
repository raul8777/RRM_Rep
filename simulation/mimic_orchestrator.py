"""MIMIC — Orchestrator entrypoint for manual and scheduled execution."""
from __future__ import annotations
import os
import sys
import argparse
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_root)

from simulation.mimic_scheduler import MimicScheduler, Phase, get_scheduler
from simulation.mimic_universe_selector import get_selector
from simulation.mimic_window_controller import get_window_controller


def _print_state(scheduler: MimicScheduler) -> None:
    state = scheduler.get_state()
    print(json.dumps({
        "phase": state.get("phase"),
        "status": state.get("status"),
        "progress_pct": state.get("progress_pct"),
        "current_micro_universe_id": state.get("current_micro_universe_id"),
        "updated_at": state.get("updated_at"),
    }, indent=2))


def cmd_start(args: argparse.Namespace) -> int:
    scheduler = get_scheduler()
    can_run = scheduler.can_run_now()
    if not can_run.get("can_run") and not args.force:
        print(f"Cannot start automatically: {can_run.get('reason')}")
        print(f"Next window: {can_run.get('next_window')}")
        return 1
    scheduler.start()
    print("MIMIC scheduler started.")
    _print_state(scheduler)
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    scheduler = get_scheduler()
    scheduler.start()
    universe = scheduler.select_next_micro_universe()
    if not universe:
        print("No pending micro universes available.")
        return 1
    print(f"Selected micro universe: {universe['id']}")
    print(json.dumps(universe, indent=2))
    return 0


def cmd_run_phase(args: argparse.Namespace) -> int:
    """Run a specific phase for the current/selected micro universe."""
    scheduler = get_scheduler()
    if args.phase == "audit":
        # Run global audit and generate queue for the most recent 21 days
        selector = get_selector()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=21)
        selector.generate_initial_queue(
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            window_days=args.window_days,
        )
        scheduler.set_phase(Phase.SELECT_UNIVERSE, "completed", progress_pct=10.0)
        print("Audit completed and initial queue generated.")
    else:
        # Direct phase execution by scheduler
        if args.phase.upper() in {p.value for p in Phase}:
            scheduler.set_phase(Phase(args.phase.upper()), "running")
        res = scheduler.run_phase()
        print(json.dumps(res, indent=2, default=str))
    _print_state(scheduler)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one full micro-universe cycle (select -> apply)."""
    scheduler = get_scheduler()
    can_run = scheduler.can_run_now()
    if not can_run.get("can_run") and not args.force:
        print(f"Cannot run automatically: {can_run.get('reason')}")
        print(f"Next window: {can_run.get('next_window')}")
        return 1
    res = scheduler.run_once()
    print(json.dumps(res, indent=2, default=str))
    return 0 if res.get("ok") else 1


def cmd_retry(args: argparse.Namespace) -> int:
    scheduler = get_scheduler()
    if args.micro_universe:
        selector = get_selector()
        selector.mark_status(args.micro_universe, "pending")
        scheduler._state["current_micro_universe_id"] = args.micro_universe
        scheduler._save_state()
        print(f"Marked {args.micro_universe} as pending for retry.")
    else:
        scheduler.resume()
        print("Scheduler resumed from failed/paused state.")
    _print_state(scheduler)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    scheduler = get_scheduler()
    scheduler.pause()
    print("MIMIC scheduler paused.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    scheduler = get_scheduler()
    scheduler.resume()
    print("MIMIC scheduler resumed.")
    _print_state(scheduler)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    scheduler = get_scheduler()
    scheduler.reset()
    print("MIMIC scheduler reset to IDLE.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    scheduler = get_scheduler()
    summary = scheduler.get_summary()
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_force_universe(args: argparse.Namespace) -> int:
    selector = get_selector()
    if selector.force_next(args.micro_universe):
        print(f"Forced next micro universe: {args.micro_universe}")
        return 0
    print(f"Micro universe not found: {args.micro_universe}")
    return 1


def cmd_set_schedule(args: argparse.Namespace) -> int:
    controller = get_window_controller()
    schedule = controller.get_schedule()
    if args.mode:
        schedule["mode"] = args.mode
    if args.window:
        # format: days,start,end e.g. "0,1,2,3,4,5,6,00:00,07:00"
        parts = args.window.split(",")
        days = [int(d) for d in parts[:-2]]
        start = parts[-2]
        end = parts[-1]
        schedule["windows"] = [{"days": days, "start": start, "end": end}]
    controller.set_schedule(schedule)
    print("Schedule updated.")
    print(json.dumps(controller.get_schedule(), indent=2))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="MIMIC orchestrator")
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="Start scheduler if window allows")
    p_start.add_argument("--force", action="store_true", help="Ignore window check")

    p_run = sub.add_parser("run", help="Run one full micro-universe cycle")
    p_run.add_argument("--force", action="store_true", help="Ignore window check")

    sub.add_parser("next", help="Select next micro universe")

    p_phase = sub.add_parser("run-phase", help="Run a specific global phase")
    p_phase.add_argument("--phase", required=True, choices=["audit", "select_universe", "download", "validate", "calibrate", "cross_validate", "apply"])
    p_phase.add_argument("--window-days", type=int, default=7, help="Micro-universe window size in days")

    p_retry = sub.add_parser("retry", help="Retry failed or specific micro universe")
    p_retry.add_argument("--micro-universe", default=None)

    sub.add_parser("pause", help="Pause scheduler")
    sub.add_parser("resume", help="Resume scheduler")
    sub.add_parser("reset", help="Reset scheduler to IDLE")
    sub.add_parser("status", help="Show scheduler status")

    p_force = sub.add_parser("force-universe", help="Force specific micro universe next")
    p_force.add_argument("--micro-universe", required=True)

    p_sched = sub.add_parser("set-schedule", help="Set execution window schedule")
    p_sched.add_argument("--mode", choices=["auto", "manual", "suggest-only"])
    p_sched.add_argument("--window", help="days,start,end e.g. '0,1,2,3,4,5,6,00:00,07:00'")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "start": cmd_start,
        "run": cmd_run,
        "next": cmd_next,
        "run-phase": cmd_run_phase,
        "retry": cmd_retry,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "reset": cmd_reset,
        "status": cmd_status,
        "force-universe": cmd_force_universe,
        "set-schedule": cmd_set_schedule,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
