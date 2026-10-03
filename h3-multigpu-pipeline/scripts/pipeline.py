#!/usr/bin/env python3
"""Unified local entry point for the packaged H3 multi-GPU pipeline."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from h3_pipeline import ROUTES, execute_run, make_plan, resume_run, run_preflight, selftest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path.home() / ".config" / "h3-multigpu-pipeline" / "local.json"
PUBLIC_CONFIG = ROOT / "config" / "public-defaults.json"


def merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("config schema_version must be 1")
    for label, host in config.get("hosts", {}).items():
        if not isinstance(host, dict):
            raise ValueError(f"host {label} must be an object")
    for route, pools in ROUTES.items():
        if config.get("routes", {}).get(route) != list(pools):
            raise ValueError(f"route definition mismatch: {route}")
    return config


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="print the action plan without SSH or HTTP requests")


def parser_for() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline.py")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="offline route and profile plan")
    plan.add_argument("--mode", choices=ROUTES, required=True)
    plan.add_argument("--width", type=int, required=True)
    plan.add_argument("--height", type=int, required=True)
    plan.add_argument("--seconds", type=int, required=True)
    plan.add_argument("--count", type=int, default=1)
    plan.add_argument("--seed", type=int, default=1)

    adapt = sub.add_parser("config-adapt", help="merge a private host map into public defaults")
    adapt.add_argument("--host-map", type=Path, required=True)
    adapt.add_argument("--out", type=Path, default=DEFAULT_CONFIG)
    adapt.add_argument("--dry-run", action="store_true")

    for name in ("preflight", "start", "status", "stop"):
        cmd = sub.add_parser(name)
        add_common(cmd)
        cmd.add_argument("--mode", choices=ROUTES, required=True, help="checks/controls only the selected sampler pools plus decoder")
        if name == "stop":
            cmd.add_argument("--run-id")

    for name in ("run", "submit"):
        cmd = sub.add_parser(name, help="require explicit prompt and reference inputs")
        add_common(cmd)
        cmd.add_argument("--mode", choices=ROUTES, required=True)
        cmd.add_argument("--prompt-file", type=Path, required=True)
        cmd.add_argument("--reference-file", type=Path, required=True)
        cmd.add_argument("--width", type=int, required=True)
        cmd.add_argument("--height", type=int, required=True)
        cmd.add_argument("--seconds", type=int, required=True)
        cmd.add_argument("--count", type=int, default=1)
        cmd.add_argument("--seed", type=int, required=True)
        cmd.add_argument("--background", action="store_true")

    wait = sub.add_parser("wait", help="wait for a local run manifest")
    wait.add_argument("run_id")
    wait.add_argument("--state-dir", type=Path, default=Path.home() / ".local" / "state" / "h3-multigpu-pipeline")
    wait.add_argument("--timeout", type=float, default=7200)
    wait.add_argument("--dry-run", action="store_true")

    sub.add_parser("selftest", help="offline mock-HTTP behavior checks")
    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--run-dir", type=Path, required=True)
    resume = sub.add_parser("resume", help="continue only confirmed saved latents or existing prompt journals")
    resume.add_argument("run_id")
    resume.add_argument("--state-dir", type=Path, default=Path.home() / ".local" / "state" / "h3-multigpu-pipeline")
    resume.add_argument("--dry-run", action="store_true")
    return parser


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parser_for().parse_args(argv)
    try:
        if args.command == "selftest":
            print_json(selftest())
            return 0
        if args.command == "plan":
            plan = make_plan(args.mode, args.width, args.height, args.seconds, args.count, args.seed)
            print_json(plan)
            return 2 if plan["profile_status"] == "known_oom" else 0
        if args.command == "config-adapt":
            base = json.loads(PUBLIC_CONFIG.read_text(encoding="utf-8"))
            local_map = json.loads(args.host_map.read_text(encoding="utf-8"))
            result = merge(base, local_map)
            if args.dry_run:
                print_json({"would_write": str(args.out), "config": result})
            else:
                if args.out.exists():
                    raise FileExistsError(f"refusing to overwrite existing config: {args.out}")
                args.out.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.out.with_suffix(args.out.suffix + ".part")
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.chmod(temporary, 0o600)
                temporary.replace(args.out)
                print(args.out)
            return 0
        if args.command == "wait":
            if args.dry_run:
                print_json({"action": "wait", "run_id": args.run_id, "state_dir": str(args.state_dir), "offline": True})
                return 0
            return wait_for_run(args.state_dir, args.run_id, args.timeout)
        if args.command == "_worker":
            result = execute_run(args.run_dir)
            print_json(result)
            return 0 if result.get("state") == "success" else 1
        if args.command == "resume":
            run_dir = args.state_dir / args.run_id
            if args.dry_run:
                print_json({"action": "resume", "run_id": args.run_id, "state_dir": str(args.state_dir), "sampling": "never_restarted", "offline": True})
                return 0
            if not run_dir.is_dir():
                raise ValueError(f"unknown local run id: {args.run_id}")
            result = resume_run(run_dir)
            print_json(result)
            return 0 if result.get("state") == "success" else 1
        config = load_config(args.config)
        if args.command == "preflight":
            outcomes = run_preflight(config, args.mode)
            if args.dry_run:
                print_json({"action": "preflight", "mode": args.mode, "outcomes": outcomes, "api_checked": False, "offline": True})
                return 0 if all(row.get("status") == "pass" for row in outcomes) else 1
            print_json({"mode": args.mode, "outcomes": outcomes, "api_checked": False, "note": "API nodes are checked by start after selected services are ready."})
            return 0 if all(row.get("status") == "pass" for row in outcomes) else 1
        if args.command == "start":
            if args.dry_run:
                print_json({"action": "start", "mode": args.mode, "pools": list(config["routes"][args.mode]), "decoder": True, "offline": True})
                return 0
            return start_services(config, args.mode)
        if args.command == "status":
            if args.dry_run:
                print_json({"action": "status", "mode": args.mode, "pools": list(config["routes"][args.mode]), "offline": True})
                return 0
            return show_status(config, args.mode)
        if args.command == "stop":
            if args.dry_run:
                print_json({"action": "stop", "mode": args.mode, "run_id": args.run_id, "owned_services_only": True, "offline": True})
                return 0
            return stop_services(config, args.mode, args.run_id)
        if args.command in ("run", "submit"):
            plan = make_plan(args.mode, args.width, args.height, args.seconds, args.count, args.seed)
            if plan["profile_status"] != "validated":
                print_json(plan)
                return 2
            if args.dry_run:
                print_json({"action": args.command, "plan": plan, "prompt_file_supplied": bool(args.prompt_file), "reference_file_supplied": bool(args.reference_file), "offline": True})
                return 0
            validate_explicit_inputs(args.prompt_file, args.reference_file)
            config = load_config(args.config)
            return launch_run(config, args, plan)
        raise AssertionError(args.command)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def validate_explicit_inputs(prompt_file: Path, reference_file: Path) -> None:
    if not prompt_file.is_file() or not prompt_file.read_text(encoding="utf-8").strip():
        raise ValueError("run requires a readable, non-empty --prompt-file")
    if not reference_file.is_file() or reference_file.stat().st_size == 0:
        raise ValueError("run requires a readable, non-empty --reference-file")


def preflight(config: dict[str, Any], mode: str) -> int:
    outcomes = run_preflight(config, mode)
    print_json(outcomes)
    return 0 if all(row.get("status") == "pass" for row in outcomes) else 1


def start_services(config: dict[str, Any], mode: str) -> int:
    from h3_pipeline import manage_services
    outcomes = manage_services(config, mode, "start")
    print_json(outcomes)
    return 0 if all(row.get("status") == "pass" for row in outcomes) else 1


def show_status(config: dict[str, Any], mode: str) -> int:
    from h3_pipeline import manage_services
    outcomes = manage_services(config, mode, "status")
    print_json(outcomes)
    return 0 if all(row.get("status") == "pass" for row in outcomes) else 1


def stop_services(config: dict[str, Any], mode: str, run_id: str | None) -> int:
    if run_id:
        state_dir = Path.home() / ".local" / "state" / "h3-multigpu-pipeline"
        run_dir = state_dir / run_id
        request = run_dir / "stop.request"
        if not run_dir.is_dir():
            raise ValueError(f"unknown local run id: {run_id}")
        request.touch(exist_ok=True)
    from h3_pipeline import manage_services
    outcomes = manage_services(config, mode, "stop")
    print_json({"run_stop_requested": run_id, "services": outcomes})
    return 0 if all(row.get("status") == "pass" for row in outcomes) else 1


def launch_run(config: dict[str, Any], args: argparse.Namespace, plan: dict[str, Any]) -> int:
    state_dir = Path.home() / ".local" / "state" / "h3-multigpu-pipeline"
    state_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{args.mode}-{plan['seeds'][0]}-{os.urandom(4).hex()}"
    run_dir = state_dir / run_id
    run_dir.mkdir(mode=0o700)
    request = {
        "run_id": run_id,
        "config": str(args.config.resolve()),
        "mode": args.mode,
        "prompt_file": str(args.prompt_file.resolve()),
        "reference_file": str(args.reference_file.resolve()),
        "plan": plan,
    }
    (run_dir / "request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"run_id": run_id, "state": "queued", "mode": args.mode, "profile": plan["profile"], "profile_status": plan["profile_status"], "quality_status": plan.get("quality_status", "not_assessed_by_pipeline"), "quality_review_required": plan.get("quality_review_required", False), "tested_batch_count": plan.get("tested_batch_count"), "long_queue_repeatability_unverified": True, "jobs": plan["jobs"]}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    worker = Path(__file__).resolve()
    command = [sys.executable, str(worker), "_worker", "--run-dir", str(run_dir)]
    if args.background:
        import subprocess
        log = (run_dir / "worker.log").open("ab")
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print_json({"run_id": run_id, "state": "queued", "manifest": str(run_dir / "manifest.json")})
        return 0
    os.execv(sys.executable, command)
    return 0


def wait_for_run(state_dir: Path, run_id: str, timeout: float) -> int:
    import time
    manifest_path = state_dir / run_id / "manifest.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not manifest_path.is_file():
            raise ValueError(f"unknown run id: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("state") in ("success", "failed", "stopped"):
            print_json(manifest)
            return 0 if manifest["state"] == "success" else 1
        time.sleep(1)
    raise TimeoutError(f"run {run_id} did not finish within {timeout:g}s")


if __name__ == "__main__":
    sys.exit(main())
