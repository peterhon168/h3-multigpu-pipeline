#!/usr/bin/env python3
"""Small, dependency-free helpers shared by the H3 pipeline command line."""

from __future__ import annotations

import concurrent.futures
import base64
import contextlib
import fcntl
import json
import os
import queue
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


ROUTES = {
    "eight_plus_one": ("t10_8",),
    "eight_xtx_plus_one": ("t10_8", "xtx"),
    "four_four_plus_one": ("t10_4a", "t10_4b"),
    "four_four_xtx_plus_one": ("t10_4a", "t10_4b", "xtx"),
}

VALIDATED = {
    (416, 736, 5): set(ROUTES),
    (736, 1280, 5): set(ROUTES),
    (736, 1280, 10): {"eight_plus_one"},
    (544, 960, 15): {"eight_plus_one"},
}
TESTED_BATCH_COUNT = {
    (416, 736, 5): 6,
    (736, 1280, 5): 6,
    (736, 1280, 10): 1,
    (544, 960, 15): 1,
}
KNOWN_OOM = (640, 1152, 15)
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REMOTE_WORKER = PACKAGE_ROOT / "scripts" / "remote_worker.py"


class ComfyError(RuntimeError):
    pass


def request_json(
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ComfyError(f"HTTP request failed for {url}: {exc}") from exc


def _status_error(status: dict[str, Any]) -> str | None:
    if str(status.get("status_str", "")).lower() == "error":
        return json.dumps(status, ensure_ascii=False)
    for message in status.get("messages", []) or []:
        if isinstance(message, (list, tuple)) and message and message[0] == "execution_error":
            return json.dumps(message[1] if len(message) > 1 else message, ensure_ascii=False)
    return None


def wait_history(
    api: str,
    prompt_id: str,
    timeout: float,
    interval: float = 1,
    request: Callable[..., dict[str, Any]] = request_json,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        payload = request(f"{api.rstrip('/')}/history/{prompt_id}", timeout=min(15, max(0.1, deadline - monotonic())))
        history = payload.get(prompt_id)
        if history:
            status = history.get("status", {}) or {}
            error = _status_error(status)
            if error is not None:
                raise ComfyError(f"ComfyUI job {prompt_id} failed: {error}")
            if status.get("completed"):
                return history
        time.sleep(min(interval, max(0, deadline - monotonic())))
    raise TimeoutError(f"ComfyUI job {prompt_id} exceeded {timeout:g}s")


def submit_and_wait(
    api: str,
    graph: dict[str, Any],
    timeout: float,
    interval: float = 1,
    request: Callable[..., dict[str, Any]] = request_json,
) -> tuple[str, dict[str, Any]]:
    submitted = request(f"{api.rstrip('/')}/prompt", {"prompt": graph})
    if submitted.get("node_errors"):
        raise ComfyError(json.dumps(submitted["node_errors"], ensure_ascii=False))
    prompt_id = submitted.get("prompt_id")
    if not prompt_id:
        raise ComfyError(f"ComfyUI submission did not return prompt_id: {submitted!r}")
    return prompt_id, wait_history(api, str(prompt_id), timeout, interval, request)


def profile_status(mode: str, width: int, height: int, seconds: int, count: int = 1) -> tuple[str, str]:
    spec = (width, height, seconds)
    if spec == KNOWN_OOM:
        return "known_oom", "Observed OOM at 640x1152/15s; this package refuses to submit it."
    tested_modes = VALIDATED.get(spec)
    if tested_modes is None:
        return "unverified", "This resolution/duration combination was not validated."
    if mode in tested_modes:
        tested_count = TESTED_BATCH_COUNT[spec]
        reason = f"Observed on the selected route and profile; tested_batch_count={tested_count}."
        if count > tested_count:
            reason += " Longer-queue repeatability is unverified; bounded queue backpressure and timeouts still apply."
        return "validated", reason
    return "unverified", "This profile was tested on another route, not this route."


def make_plan(mode: str, width: int, height: int, seconds: int, count: int, seed: int) -> dict[str, Any]:
    if mode not in ROUTES:
        raise ValueError(f"unknown mode: {mode}")
    if count < 1:
        raise ValueError("count must be positive")
    if not 0 <= seed < 2**64 or seed + count - 1 >= 2**64:
        raise ValueError("seeds must remain in [0, 2**64)")
    status, reason = profile_status(mode, width, height, seconds, count)
    quality_status = "pending_human_review" if (mode, width, height, seconds) == ("four_four_xtx_plus_one", 736, 1280, 5) else "not_assessed_by_pipeline"
    pools = ROUTES[mode]
    jobs = [
        {"seed": seed + index, "pool": pools[index % len(pools)], "status": "planned"}
        for index in range(count)
    ]
    return {
        "mode": mode,
        "profile": {"width": width, "height": height, "seconds": seconds},
        "profile_status": status,
        "quality_status": quality_status,
        "quality_review_required": quality_status == "pending_human_review",
        "reason": reason,
        "count": count,
        "tested_batch_count": TESTED_BATCH_COUNT.get((width, height, seconds)),
        "long_queue_repeatability_unverified": True,
        "count_exceeds_tested_batch": count > TESTED_BATCH_COUNT.get((width, height, seconds), 0),
        "seeds": [job["seed"] for job in jobs],
        "sampler_pools": list(pools),
        "shared_decoder": "decoder",
        "jobs": jobs,
    }


def selected_services(config: dict[str, Any], mode: str) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    if mode not in ROUTES:
        raise ValueError(f"unknown mode: {mode}")
    selected = []
    for pool_name in config["routes"][mode]:
        pool = config["pools"][pool_name]
        selected.append((pool_name, config["hosts"][pool["host"]], pool))
    selected.append(("decoder", config["hosts"][config["decoder"]["host"]], config["decoder"]))
    return selected


def run_preflight(config: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    """Static, offline checks only. API/model checks are deliberately post-start."""
    outcomes = []
    for label, host, service in selected_services(config, mode):
        problems = []
        target = host.get("ssh_target") or host.get("ssh_alias")
        if not isinstance(target, str) or not target or "REPLACE_" in target or "SET_LOCAL_" in target:
            problems.append("ssh_target is missing or still a public placeholder")
        options = host.get("ssh_options", [])
        if not isinstance(options, list) or not all(isinstance(arg, str) for arg in options):
            problems.append("ssh_options must be a list of strings")
        for key in ("comfy_root", "python", "input_dir", "run_root", "api_host"):
            if not isinstance(host.get(key), str) or not host[key]:
                problems.append(f"host.{key} is required")
        for key in ("launch_prefix", "extra_args"):
            value = host.get(key, [])
            if isinstance(value, str):
                continue
            if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
                problems.append(f"host.{key} must be a string or list of strings")
        if not isinstance(host.get("env", {}), dict):
            problems.append("host.env must be an object")
        elif not all(isinstance(key, str) and isinstance(value, str) for key, value in host.get("env", {}).items()):
            problems.append("host.env keys and values must be strings")
        for key in ("unset_env", "handoff_ssh_options"):
            value = host.get(key, [])
            if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
                problems.append(f"host.{key} must be a list of strings")
        workflow = service.get("workflow")
        if label != "decoder" and workflow:
            if not _resolve_package_path(workflow).is_file():
                problems.append(f"workflow file does not exist: {workflow}")
        if label == "decoder" and workflow and not _resolve_package_path(workflow).is_file():
            problems.append(f"decoder workflow file does not exist: {workflow}")
        if not isinstance(service.get("port"), int) or not 1 <= service["port"] <= 65535:
            problems.append("service port must be an integer in 1..65535")
        outcomes.append({"service": label, "status": "fail" if problems else "pass", "checks": problems or ["static configuration"], "api_checked": False})
    selected_labels = {label for label, _host, _service in selected_services(config, mode)}
    if "t10_8" in selected_labels or "t10_4a" in selected_labels or "t10_4b" in selected_labels:
        transfer = config.get("decoder", {}).get("transfer", {})
        targets = transfer.get("targets", {})
        choices = [transfer.get("preferred", "10gbe"), transfer.get("fallback")]
        if not any(targets.get(name) and not str(targets[name]).startswith(("SET_LOCAL_", "REPLACE_")) for name in choices if name):
            outcomes.append({"service": "t10-handoff", "status": "fail", "checks": ["configure a direct decoder target (10gbe and/or lan fallback)"], "api_checked": False})
    if "xtx" in selected_labels and not config["hosts"]["xtx"].get("same_host_as_decoder"):
        outcomes.append({"service": "xtx-handoff", "status": "fail", "checks": ["XTX route requires same_host_as_decoder=true; Mac relay is disabled"], "api_checked": False})
    return outcomes


def _resolve_package_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    first = PACKAGE_ROOT / candidate
    if first.exists():
        return first
    return (PACKAGE_ROOT / "config" / candidate).resolve()


def _argv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return list(value)
    raise ValueError("expected a string or a list of strings")


def remote_rpc(
    host: dict[str, Any],
    action: str,
    spec: dict[str, Any],
    timeout: float = 3600,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the self-contained helper on one configured host over its own SSH path."""
    target = host.get("ssh_target") or host.get("ssh_alias")
    if not target:
        raise ValueError("remote host requires ssh_target (or ssh_alias)")
    options = host.get("ssh_options", [])
    if not isinstance(options, list) or not all(isinstance(arg, str) for arg in options):
        raise ValueError("ssh_options must be a list of strings")
    python = host.get("python") or "python3"
    if not Path(python).is_absolute() and host.get("comfy_root"):
        python = f"{host['comfy_root'].rstrip('/')}/{python}"
    source = REMOTE_WORKER.read_text(encoding="utf-8")
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    bootstrap = f"import base64;exec(compile(base64.b64decode('{encoded}'),'<h3-remote-worker>','exec'))"
    command = shlex.join([python, "-c", bootstrap, action])
    argv = ["ssh", "-T", *options, "--", target, command]
    try:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = child.communicate(json.dumps(spec, ensure_ascii=False).encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        child.kill()
        stdout, stderr = child.communicate()
        _dispatch_remote_events(stdout, on_event)
        raise TimeoutError(f"remote {action} exceeded {timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start SSH for remote {action}: {exc}") from exc
    _dispatch_remote_events(stdout, on_event)
    if child.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"remote {action} failed (exit {child.returncode}): {detail}")
    rows = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if not rows:
        raise RuntimeError(f"remote {action} returned no JSON response")
    try:
        response = json.loads(rows[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"remote {action} returned invalid JSON: {rows[-1][:500]}") from exc
    if response.get("event") == "result":
        return response.get("data", {})
    return response


def _dispatch_remote_events(stdout: bytes, on_event: Callable[[dict[str, Any]], None] | None) -> None:
    if on_event is None:
        return
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "submitted":
            on_event(event)


def _service_spec(config: dict[str, Any], label: str, host: dict[str, Any], service: dict[str, Any]) -> dict[str, Any]:
    comfy_root = host["comfy_root"]
    python = host["python"]
    if not Path(python).is_absolute():
        python = str(Path(comfy_root) / python)
    run_root = host["run_root"]
    port = int(service["port"])
    service_root = f"{run_root.rstrip('/')}/services/{label}"
    input_dir = host["input_dir"]
    output_dir = f"{service_root}/output"
    temp_dir = f"{service_root}/tmp"
    user_dir = f"{service_root}/user"
    tmp_short = f"/tmp/h3-r-{label.replace('_', '-')[:16]}"
    is_xtx = service.get("kind") == "xtx" or label == "xtx"
    env = {**host.get("env", {})}
    unset = list(host.get("unset_env", []))
    if is_xtx:
        env.update({"HIP_VISIBLE_DEVICES": str(service.get("gpu_ids", "0"))})
        unset.extend(["CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"])
    else:
        env.update({"CUDA_VISIBLE_DEVICES": str(service.get("gpu_ids", "0"))})
        unset.extend(["HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"])
    env.update({
        "MASTER_ADDR": "127.0.0.1",
        "MASTER_PORT": str(service.get("master_port") or (29600 + port % 1000)),
        "RAYLIGHT_RAY_TMPDIR": tmp_short,
        "RAY_TMPDIR": tmp_short,
        "TMPDIR": temp_dir,
        "H3_MP_RETAIN_CPU_WEIGHTS": "1",
        "TORCH_EFFICIENT": "1",
    })
    for key in set(unset):
        env.pop(key, None)
    launch = _argv(host.get("launch_prefix", []))
    extra = _argv(host.get("extra_args", [])) + _argv(service.get("extra_args", []))
    if label == "decoder":
        extra = ["--disable-all-custom-nodes", "--whitelist-custom-nodes", "h3_latent_handoff", *extra]
    argv = [*launch, python, "main.py", "--listen", str(host.get("api_host", "127.0.0.1")), "--port", str(port), "--input-directory", input_dir, "--output-directory", output_dir, "--temp-directory", temp_dir, "--user-directory", user_dir, "--database-url", f"sqlite:///{user_dir}/comfyui.db", *extra]
    return {
        "action": "start",
        "label": label,
        "state_path": f"{service_root}/service-owner.json",
        "expected_executable": python,
        "cwd": comfy_root,
        "argv": argv,
        "environment": env,
        "unset": sorted(set(unset)),
        "directories": [service_root, input_dir, output_dir, user_dir, temp_dir, tmp_short],
        "log_path": f"{service_root}/comfy.log",
        "start_timeout": config["timeouts"]["service_start_seconds"],
        "stop_timeout": config["timeouts"]["service_stop_seconds"],
        "api_host": host.get("api_host", "127.0.0.1"),
        "port": port,
    }


def _required_nodes(config: dict[str, Any], label: str) -> list[str]:
    if label == "decoder":
        graph = json.loads(_resolve_package_path(config["decoder"]["workflow"]).read_text(encoding="utf-8"))
    else:
        graph = _load_workflow(config, label)
    return sorted({str(node["class_type"]) for node in graph.values() if isinstance(node, dict) and node.get("class_type")})


def _required_models(config: dict[str, Any], label: str) -> list[dict[str, str]]:
    graph = json.loads(_resolve_package_path(config["decoder"]["workflow"]).read_text(encoding="utf-8")) if label == "decoder" else _load_workflow(config, label)
    model_fields = {"vae_name", "clip_name", "unet_name", "lora_name"}
    requirements = []
    for node in graph.values():
        if not isinstance(node, dict) or not node.get("class_type"):
            continue
        for key, value in node.get("inputs", {}).items():
            if key in model_fields and isinstance(value, str) and value:
                requirements.append({"class_type": node["class_type"], "input": key, "name": value})
    return requirements


def manage_services(config: dict[str, Any], mode: str, action: str) -> list[dict[str, Any]]:
    if action not in ("start", "status", "stop"):
        raise ValueError(f"unsupported service action: {action}")
    checks = run_preflight(config, mode)
    failures = [item for item in checks if item["status"] != "pass"]
    if failures:
        return failures
    outcomes: list[dict[str, Any]] = []
    selected = selected_services(config, mode)
    for label, host, service in selected:
        spec = _service_spec(config, label, host, service)
        spec["action"] = action
        try:
            result = remote_rpc(host, "service", spec, timeout=config["timeouts"].get("ssh_seconds", 30) + config["timeouts"].get("service_start_seconds", 120) + 30)
            service_state = result.get("status")
            accepted = (
                service_state in ("started", "already_running") if action == "start"
                else service_state == "running" if action == "status"
                else service_state in ("stopped", "not_running", "stale_state_removed")
            )
            row = {"service": label, "status": "pass" if accepted else "fail", "result": result}
            if not accepted:
                row["error"] = f"unexpected service state for {action}: {service_state}"
            if action == "start":
                api_spec = {
                    "api": f"http://{host.get('api_host', '127.0.0.1')}:{service['port']}",
                    "required_nodes": _required_nodes(config, label),
                    "required_models": _required_models(config, label),
                    "http_timeout": config["timeouts"].get("http_seconds", 15),
                }
                api_result = remote_rpc(host, "preflight", api_spec, timeout=config["timeouts"].get("http_seconds", 15) + 10)
                row["api_validation"] = api_result
                row["api_checked_after_start"] = True
                if row["status"] == "pass" and api_result.get("status") != "pass":
                    row["status"] = "fail"
                    row["error"] = "post-start API object_info validation failed"
            outcomes.append(row)
        except (RuntimeError, TimeoutError, ValueError) as exc:
            outcomes.append({"service": label, "status": "fail", "error": f"{type(exc).__name__}: {exc}"})
            if action == "start":
                break
    return outcomes


def _load_workflow(config: dict[str, Any], entry: str) -> dict[str, Any]:
    workflow = _resolve_package_path(config["pools"][entry]["workflow"])
    return json.loads(workflow.read_text(encoding="utf-8"))


def _set_node_input(graph: dict[str, Any], class_type: str, input_name: str, value: Any) -> None:
    matches = [node for node in graph.values() if node.get("class_type") == class_type]
    if len(matches) != 1:
        raise ValueError(f"workflow must contain exactly one {class_type} node; found {len(matches)}")
    matches[0].setdefault("inputs", {})[input_name] = value


def build_sampler_workflow(config: dict[str, Any], pool_name: str, prompt: str, reference: str, width: int, height: int, seconds: int, seed: int, run_id: str) -> dict[str, Any]:
    graph = _load_workflow(config, pool_name)
    pool = config["pools"][pool_name]
    if pool.get("kind") == "t10":
        _set_node_input(graph, "PrimitiveStringMultiline", "value", prompt)
        _set_node_input(graph, "LoadImage", "image", reference)
        _set_node_input(graph, "PrimitiveFloat", "value", float(seconds))
        ref = next(node for node in graph.values() if node.get("class_type") == "MiniMaxH3ReferenceToVideo")
        ref["inputs"]["width"] = width
        ref["inputs"]["height"] = height
        initializer = next(node for node in graph.values() if node.get("class_type") == "RayInitializer")
        initializer["inputs"].update({"GPU": int(pool["gpu_count"]), "ulysses_degree": int(pool["ulysses_degree"]), "ring_degree": int(pool["ring_degree"])})
        clip = next(node for node in graph.values() if node.get("class_type") == "H3MultiGPUCLIPLoader")
        # CUDA_VISIBLE_DEVICES remaps physical ids into process-local 0..N-1.
        clip["inputs"]["gpu_ids"] = ",".join(str(index) for index in range(int(pool["gpu_count"])))
        sampler = next(node for node in graph.values() if node.get("class_type") == "XFuserSamplerCustomAdvanced")
        sampler["inputs"]["noise_seed"] = seed
        save = next(node for node in graph.values() if node.get("class_type") == "SaveH3Latent")
    else:
        _set_node_input(graph, "PrimitiveStringMultiline", "value", prompt)
        _set_node_input(graph, "LoadImage", "image", reference)
        ref = next(node for node in graph.values() if node.get("class_type") == "MiniMaxH3ReferenceToVideo")
        ref["inputs"].update({"width": width, "height": height, "length": ((max(5, round(seconds * 24)) + (5 - (max(5, round(seconds * 24)) % 17)) % 17))})
        sampler = next(node for node in graph.values() if node.get("class_type") == "RandomNoise")
        sampler["inputs"]["noise_seed"] = seed
        save = next(node for node in graph.values() if node.get("class_type") == "SaveH3Latent")
    save["inputs"]["filename_prefix"] = f"h3_handoff/{run_id}/seed_{seed}"
    save_id = next(node_id for node_id, node in graph.items() if node is save)
    if save_id != "save":
        graph["save"] = graph.pop(save_id)
    return graph


def build_decoder_workflow(config: dict[str, Any], latent_name: str, run_id: str, seed: int, profile: dict[str, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    decoder = config["decoder"]
    workflow_path = _resolve_package_path(decoder["workflow"])
    graph = json.loads(workflow_path.read_text(encoding="utf-8"))
    loaders = [(node_id, node) for node_id, node in graph.items() if node.get("class_type") == "LoadH3Latent"]
    saves = [(node_id, node) for node_id, node in graph.items() if node.get("class_type") == "SaveVideo"]
    if len(loaders) != 1 or len(saves) != 1:
        raise ValueError("decoder workflow must contain exactly one LoadH3Latent and one SaveVideo node")
    _set_node_input(graph, "CreateVideo", "fps", int(decoder.get("fps", 24)))
    load_id, load_node = loaders[0]
    load_inputs = load_node.setdefault("inputs", {})
    load_key = next((key for key in ("latent", "filename", "latent_file") if key in load_inputs), "latent")
    load_inputs[load_key] = latent_name
    vaes = [node for node in graph.values() if node.get("class_type") == "VAELoader"]
    if len(vaes) >= 2:
        for node in vaes:
            vae_name = str(node.get("inputs", {}).get("vae_name", "")).lower()
            if "audio" in vae_name:
                node["inputs"]["vae_name"] = decoder["audio_vae"]
            else:
                node["inputs"]["vae_name"] = decoder["video_vae"]
    save_id, save_node = saves[0]
    save_node.setdefault("inputs", {})["filename_prefix"] = f"h3_pipeline/{run_id}/seed_{seed}"
    fps = int(decoder.get("fps", 24))
    frames = max(5, round(int(profile["seconds"]) * fps))
    frames += (5 - frames % 17) % 17
    save_spec = {
        "node_id": str(save_id),
        "class_type": "SaveVideo",
        "expected_media": {"width": profile["width"], "height": profile["height"], "frames": frames},
        "ffmpeg_timeout": int(decoder.get("ffmpeg_timeout_seconds", 600)),
    }
    return graph, save_spec


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".part-{uuid.uuid4().hex[:8]}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _api_for(host: dict[str, Any], service: dict[str, Any]) -> str:
    return f"http://{host.get('api_host', '127.0.0.1')}:{int(service['port'])}"


def _remote_job_paths(host: dict[str, Any], run_id: str, seed: int) -> tuple[str, str]:
    base = f"{host['run_root'].rstrip('/')}/jobs/{run_id}/seed-{seed}"
    return base + "-sampler.json", base + "-decoder.json"


def _service_dir(host: dict[str, Any], label: str, child: str) -> str:
    return f"{host['run_root'].rstrip('/')}/services/{label}/{child}"


@contextlib.contextmanager
def local_worker_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".pipeline-worker.lock"
    with lock_path.open("a+") as lock_file:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another run/resume worker holds the local pipeline lock") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _service_result_ok(outcomes: list[dict[str, Any]]) -> bool:
    return bool(outcomes) and all(row.get("status") == "pass" for row in outcomes)


def _transport_latent(config: dict[str, Any], source_role: str, latent: dict[str, Any], run_id: str, seed: int) -> dict[str, Any]:
    decoder = config["decoder"]
    decoder_host = config["hosts"][decoder["host"]]
    input_root = decoder_host["input_dir"]
    relative = f"h3_pipeline/{run_id}/seed-{seed}.h3latent"
    source_host = config["hosts"][source_role]
    source_spec = {"source_path": latent["path"], "sha256": latent["sha256"], "decoder_input_root": input_root, "relative_path": relative}
    if source_role == decoder["host"] or source_host.get("same_host_as_decoder"):
        result = remote_rpc(source_host, "handoff-local", source_spec, timeout=float(config.get("timeouts", {}).get("transfer_seconds", 900)) + 30)
        return {**result, "transport": "same_host_atomic_copy", "configured_target": "same_host"}
    if source_role != "t10":
        raise RuntimeError(f"no configured direct transport from {source_role} to decoder; coordinator relay is disabled")
    transfer = decoder.get("transfer", {})
    targets = transfer.get("targets", {})
    preferred = transfer.get("preferred", "10gbe")
    order = [preferred]
    fallback = transfer.get("fallback")
    if fallback and fallback != preferred:
        order.append(fallback)
    target_errors = []
    receiver_code = base64.b64encode(REMOTE_WORKER.read_bytes()).decode("ascii")
    for target_label in order:
        target = targets.get(target_label)
        if not target or str(target).startswith(("SET_LOCAL_", "REPLACE_")):
            target_errors.append(f"{target_label}: target missing or placeholder")
            continue
        spec = {
            **source_spec,
            "decoder_target": target,
            "handoff_ssh_options": source_host.get("handoff_ssh_options", []),
            "receiver_code_b64": receiver_code,
            "child_timeout": config.get("timeouts", {}).get("transfer_seconds", 900),
        }
        try:
            result = remote_rpc(source_host, "handoff-source", spec, timeout=float(spec["child_timeout"]) + 45)
            return {**result, "transport": "source_to_decoder_ssh", "configured_target": target_label}
        except (RuntimeError, TimeoutError) as exc:
            target_errors.append(f"{target_label}: {type(exc).__name__}: {exc}")
    raise RuntimeError("direct T10-to-decoder handoff failed; no Mac relay attempted: " + "; ".join(target_errors))


def _build_decode_spec(config: dict[str, Any], run_id: str, job: dict[str, Any], latent_name: str, journal_path: str) -> dict[str, Any]:
    decoder = config["decoder"]
    host = config["hosts"][decoder["host"]]
    graph, save_node = build_decoder_workflow(config, latent_name, run_id, job["seed"], job["profile"])
    return {
        "api": _api_for(host, decoder),
        "graph": graph,
        "timeout": config["timeouts"].get("decode_seconds", decoder.get("decode_timeout_seconds", 1800)),
        "poll_interval": config["timeouts"].get("poll_seconds", 1),
        "run_id": run_id,
        "seed": job["seed"],
        "kind": "decoder",
        "journal_path": journal_path,
        "save_node": save_node,
        "output_root": _service_dir(host, "decoder", "output"),
    }


def _ensure_decoder(config: dict[str, Any]) -> None:
    decoder = config["decoder"]
    host = config["hosts"][decoder["host"]]
    spec = _service_spec(config, "decoder", host, decoder)
    spec["action"] = "start"
    remote_rpc(host, "service", spec, timeout=config["timeouts"].get("service_start_seconds", 120) + 30)
    remote_rpc(host, "preflight", {"api": _api_for(host, decoder), "required_nodes": _required_nodes(config, "decoder"), "required_models": _required_models(config, "decoder"), "http_timeout": config["timeouts"].get("http_seconds", 15)}, timeout=config["timeouts"].get("http_seconds", 15) + 10)


def execute_run(run_dir: Path) -> dict[str, Any]:
    try:
        with local_worker_lock(run_dir.parent):
            return _execute_run_unlocked(run_dir)
    except BaseException as exc:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({"state": "failed", "error": f"{type(exc).__name__}: {exc}", "completed_epoch": time.time()})
            _write_json_atomic(manifest_path, manifest)
        raise


def _execute_run_unlocked(run_dir: Path) -> dict[str, Any]:
    request_path = run_dir / "request.json"
    manifest_path = run_dir / "manifest.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    config = json.loads(Path(request["config"]).read_text(encoding="utf-8"))
    plan = request["plan"]
    if plan.get("profile_status") != "validated":
        raise RuntimeError(f"refusing unverified profile/mode: {plan.get('profile_status')} ({plan.get('reason')})")
    prompt_path = Path(request["prompt_file"])
    reference_path = Path(request["reference_file"])
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    reference_bytes = reference_path.read_bytes()
    if not prompt or not reference_bytes:
        raise ValueError("explicit prompt and reference must be non-empty")
    run_id = request["run_id"]
    mode = request["mode"]
    selected = selected_services(config, mode)
    check_rows = run_preflight(config, mode)
    if not _service_result_ok(check_rows):
        raise RuntimeError("static preflight failed: " + json.dumps(check_rows, ensure_ascii=False))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {"run_id": run_id, "jobs": plan["jobs"]}
    manifest.update({"state": "starting", "mode": mode, "profile": plan["profile"], "profile_status": plan["profile_status"], "quality_status": plan.get("quality_status", "not_assessed_by_pipeline"), "quality_review_required": plan.get("quality_review_required", False), "tested_batch_count": plan.get("tested_batch_count"), "long_queue_repeatability_unverified": True, "transport": []})
    _write_json_atomic(manifest_path, manifest)
    services = manage_services(config, mode, "start")
    manifest["services"] = services
    if not _service_result_ok(services):
        manifest["state"] = "failed"
        manifest["error"] = "service start or post-start API validation failed"
        _write_json_atomic(manifest_path, manifest)
        return manifest

    # Sampler processes on the same host share the configured host.input_dir.
    reference_by_role: dict[str, str] = {}
    for pool_name in dict.fromkeys(job["pool"] for job in plan["jobs"]):
        pool = config["pools"][pool_name]
        role = pool["host"]
        if role in reference_by_role:
            continue
        host = config["hosts"][role]
        reference_by_role[role] = remote_rpc(host, "upload-reference", {
            "api": _api_for(host, pool), "run_id": run_id,
            "filename": reference_path.name, "data_b64": base64.b64encode(reference_bytes).decode("ascii"),
            "http_timeout": config["timeouts"].get("http_seconds", 15),
        }, timeout=config["timeouts"].get("http_seconds", 15) + 20)["image"]

    abort = threading.Event()
    manifest_lock = threading.Lock()
    def update_job(seed: int, **fields: Any) -> None:
        with manifest_lock:
            for row in manifest["jobs"]:
                if int(row["seed"]) == int(seed):
                    row.update(fields)
                    break
            _write_json_atomic(manifest_path, manifest)

    decoder_host = config["hosts"][config["decoder"]["host"]]
    decoder_timeout = float(config["decoder"].get("decode_timeout_seconds", config["timeouts"].get("decode_seconds", 1800)))
    def decode_one(item: dict[str, Any]) -> dict[str, Any]:
        if (run_dir / "stop.request").exists():
            abort.set()
            raise RuntimeError("run stop was requested before decode submission")
        job = item["job"]
        latent = job["latent"]
        transport = _transport_latent(config, config["pools"][job["pool"]]["host"], latent, run_id, int(job["seed"]))
        update_job(job["seed"], state="latent_transferred", transport=transport)
        with manifest_lock:
            manifest.setdefault("transport", []).append({"seed": job["seed"], **transport})
            _write_json_atomic(manifest_path, manifest)
        decoder_journal = job["decoder_journal_path"]
        decode_spec = _build_decode_spec(config, run_id, {**job, "profile": plan["profile"]}, transport["relative_path"], decoder_journal)
        update_job(job["seed"], state="decode_submitting", decode_attempted=True)
        def record_submit(event: dict[str, Any]) -> None:
            update_job(job["seed"], state="decode_submitted", decode_prompt_id=event.get("prompt_id"))
        result = remote_rpc(decoder_host, "submit-decode", decode_spec, timeout=decoder_timeout + 60, on_event=record_submit)
        update_job(job["seed"], state="success", decode_prompt_id=result.get("prompt_id"), media=result.get("media"), outputs=result.get("outputs"))
        return result

    queue_log = run_dir / "decoder-queue.jsonl"
    decoder_queue = DecodeQueue(
        decode_one,
        max_pending=int(config["decoder"].get("max_pending", 2)),
        enqueue_timeout=float(config["decoder"].get("enqueue_timeout_seconds", 900)),
        log_path=queue_log,
        on_failure=lambda _exc: abort.set(),
    )
    futures: list[tuple[int, concurrent.futures.Future[Any]]] = []
    first_errors: list[str] = []
    by_pool: dict[str, list[dict[str, Any]]] = {}
    for job in plan["jobs"]:
        by_pool.setdefault(job["pool"], []).append(job)

    def sample_pool(pool_name: str, jobs: list[dict[str, Any]]) -> None:
        pool = config["pools"][pool_name]
        role = pool["host"]
        host = config["hosts"][role]
        for planned in jobs:
            if abort.is_set() or (run_dir / "stop.request").exists():
                break
            seed = int(planned["seed"])
            sampler_journal, _ = _remote_job_paths(host, run_id, seed)
            _, decoder_journal = _remote_job_paths(decoder_host, run_id, seed)
            job = {**planned, "profile": plan["profile"], "latent": None, "sampler_journal_path": sampler_journal, "decoder_journal_path": decoder_journal}
            update_job(seed, state="sampler_submitting", sampler_journal_path=sampler_journal, decoder_journal_path=decoder_journal)
            graph = build_sampler_workflow(config, pool_name, prompt, reference_by_role[role], plan["profile"]["width"], plan["profile"]["height"], plan["profile"]["seconds"], seed, run_id)
            spec = {
                "api": _api_for(host, pool), "graph": graph,
                "timeout": config["timeouts"].get("sampler_seconds", 5400),
                "poll_interval": config["timeouts"].get("poll_seconds", 1),
                "run_id": run_id, "seed": seed, "kind": "sampler",
                "journal_path": sampler_journal, "output_node": "save",
                "output_root": _service_dir(host, pool_name, "output"),
            }
            def record_submit(event: dict[str, Any]) -> None:
                update_job(seed, state="sampler_submitted", sampler_prompt_id=event.get("prompt_id"))
            try:
                sampled = remote_rpc(host, "submit-sampler", spec, timeout=float(spec["timeout"]) + 60, on_event=record_submit)
                latent = sampled.get("latent")
                if not latent:
                    raise RuntimeError("sampler completed without a validated latent record")
                job["latent"] = latent
                update_job(seed, state="latent_saved", latent=latent, sampler_prompt_id=sampled.get("prompt_id"))
                update_job(seed, state="decode_queued")
                future = decoder_queue.submit({"job": job})
                futures.append((seed, future))
            except BaseException as exc:
                update_job(seed, state="unknown" if not job.get("latent") else "failed", error=f"{type(exc).__name__}: {exc}")
                abort.set()
                raise

    manifest["state"] = "running"
    _write_json_atomic(manifest_path, manifest)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(by_pool))) as executor:
            workers = [executor.submit(sample_pool, pool_name, jobs) for pool_name, jobs in by_pool.items()]
            for worker in concurrent.futures.as_completed(workers):
                try:
                    worker.result()
                except BaseException as exc:
                    first_errors.append(f"{type(exc).__name__}: {exc}")
                    abort.set()
        drain_timeout = max(30.0, (decoder_timeout + float(config.get("timeouts", {}).get("transfer_seconds", 900)) + 60) * max(1, len(futures)))
        decoder_queue.close(timeout=drain_timeout)
        for seed, future in futures:
            try:
                future.result(timeout=1)
            except BaseException as exc:
                first_errors.append(f"seed {seed}: {type(exc).__name__}: {exc}")
                update_job(seed, state="failed", error=f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        try:
            decoder_queue.close(timeout=5)
        except BaseException as close_error:
            first_errors.append(f"decoder queue close: {type(close_error).__name__}: {close_error}")
        first_errors.append(f"{type(exc).__name__}: {exc}")

    stopped = (run_dir / "stop.request").exists()
    with manifest_lock:
        states = [row.get("state") for row in manifest["jobs"]]
        manifest["state"] = "stopped" if stopped else ("success" if states and all(state == "success" for state in states) and not first_errors else "failed")
        if first_errors:
            manifest["errors"] = first_errors
        manifest["completed_epoch"] = time.time()
        _write_json_atomic(manifest_path, manifest)
    return manifest


def resume_run(run_dir: Path) -> dict[str, Any]:
    with local_worker_lock(run_dir.parent):
        return _resume_run_unlocked(run_dir)


def _resume_run_unlocked(run_dir: Path) -> dict[str, Any]:
    """Recover submitted jobs and decode only latents already proven on disk."""
    request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = json.loads(Path(request["config"]).read_text(encoding="utf-8"))
    if manifest.get("profile_status") != "validated":
        raise RuntimeError("resume refuses unverified profile/mode")
    _ensure_decoder(config)
    mode = request["mode"]
    run_id = request["run_id"]
    decoder = config["decoder"]
    decoder_host = config["hosts"][decoder["host"]]
    lock = threading.Lock()
    def update(seed: int, **fields: Any) -> None:
        with lock:
            for row in manifest.get("jobs", []):
                if int(row["seed"]) == int(seed):
                    row.update(fields)
                    break
            _write_json_atomic(manifest_path, manifest)

    pending: list[dict[str, Any]] = []
    errors = []
    for row in manifest.get("jobs", []):
        if row.get("state") == "success":
            continue
        seed = int(row["seed"])
        profile = manifest["profile"]
        row["profile"] = profile
        if row.get("decode_attempted") or row.get("decode_prompt_id") or row.get("state") in ("decode_submitting", "decode_submitted", "decode_unknown"):
            try:
                spec = _build_decode_spec(config, run_id, row, "", row["decoder_journal_path"])
                spec["timeout"] = config["timeouts"].get("decode_seconds", decoder.get("decode_timeout_seconds", 1800))
                recovered = remote_rpc(decoder_host, "resume-job", spec, timeout=float(spec["timeout"]) + 30)
                if recovered.get("state") == "decode_success":
                    update(seed, state="success", decode_prompt_id=recovered.get("prompt_id"), media=recovered.get("media"), outputs=recovered.get("outputs"))
                    continue
                errors.append(f"seed {seed}: decoder journal did not resolve to success")
                update(seed, state="unknown", error="decode_job_not_confirmed")
                continue
            except BaseException as exc:
                errors.append(f"seed {seed}: {type(exc).__name__}: {exc}")
                update(seed, state="unknown", error=f"{type(exc).__name__}: {exc}")
                continue
        latent = row.get("latent")
        if not latent and row.get("sampler_journal_path"):
            pool = config["pools"][row["pool"]]
            sampler_host = config["hosts"][pool["host"]]
            try:
                sampled = remote_rpc(sampler_host, "resume-job", {
                    "api": _api_for(sampler_host, pool), "journal_path": row["sampler_journal_path"],
                    "output_node": "save", "output_root": _service_dir(sampler_host, row["pool"], "output"),
                    "timeout": config["timeouts"].get("sampler_seconds", 5400), "poll_interval": config["timeouts"].get("poll_seconds", 1),
                }, timeout=float(config["timeouts"].get("sampler_seconds", 5400)) + 30)
                latent = sampled.get("latent")
                if latent:
                    update(seed, state="latent_saved", latent=latent)
            except BaseException as exc:
                errors.append(f"seed {seed}: sampler recovery: {type(exc).__name__}: {exc}")
                update(seed, state="unknown", error=f"sampler recovery: {type(exc).__name__}: {exc}")
                continue
        if not latent:
            reason = "no confirmed saved latent; sampling will not be retried"
            errors.append(f"seed {seed}: {reason}")
            update(seed, state="unknown", error=reason)
            continue
        row["latent"] = latent
        row["profile"] = profile
        row.setdefault("decoder_journal_path", _remote_job_paths(decoder_host, run_id, seed)[1])
        pending.append(row)

    abort = threading.Event()
    def decode_saved(job: dict[str, Any]) -> dict[str, Any]:
        transport = _transport_latent(config, config["pools"][job["pool"]]["host"], job["latent"], run_id, int(job["seed"]))
        update(job["seed"], state="latent_transferred", transport=transport)
        spec = _build_decode_spec(config, run_id, job, transport["relative_path"], job["decoder_journal_path"])
        update(job["seed"], state="decode_submitting", decode_attempted=True)
        def record(event: dict[str, Any]) -> None:
            update(job["seed"], state="decode_submitted", decode_prompt_id=event.get("prompt_id"))
        result = remote_rpc(decoder_host, "submit-decode", spec, timeout=float(spec["timeout"]) + 60, on_event=record)
        update(job["seed"], state="success", decode_prompt_id=result.get("prompt_id"), media=result.get("media"), outputs=result.get("outputs"))
        return result
    queue_worker = DecodeQueue(
        lambda item: decode_saved(item["job"]),
        max_pending=int(decoder.get("max_pending", 2)),
        enqueue_timeout=float(decoder.get("enqueue_timeout_seconds", 900)),
        log_path=run_dir / "decoder-queue-resume.jsonl",
        on_failure=lambda _exc: abort.set(),
    )
    futures = []
    for job in pending:
        try:
            update(job["seed"], state="decode_queued")
            futures.append((int(job["seed"]), queue_worker.submit({"job": job})))
        except BaseException as exc:
            errors.append(f"seed {job['seed']}: {type(exc).__name__}: {exc}")
            update(job["seed"], state="failed", error=f"{type(exc).__name__}: {exc}")
            break
    try:
        queue_worker.close(timeout=max(30, (float(decoder.get("decode_timeout_seconds", 1800)) + float(config.get("timeouts", {}).get("transfer_seconds", 900)) + 30) * max(1, len(futures))))
        for seed, future in futures:
            try:
                future.result(timeout=1)
            except BaseException as exc:
                errors.append(f"seed {seed}: {type(exc).__name__}: {exc}")
                update(seed, state="failed", error=f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        errors.append(f"resume decoder queue: {type(exc).__name__}: {exc}")
    states = [row.get("state") for row in manifest.get("jobs", [])]
    manifest["state"] = "success" if states and all(state == "success" for state in states) and not errors else "failed"
    if errors:
        manifest["errors"] = errors
    manifest["resumed_epoch"] = time.time()
    _write_json_atomic(manifest_path, manifest)
    return manifest


class DecodeQueue:
    """One decoder consumer with a bounded pending queue and JSONL outcomes."""

    def __init__(
        self,
        processor: Callable[[dict[str, Any]], Any],
        max_pending: int,
        enqueue_timeout: float,
        log_path: Path | None = None,
        on_failure: Callable[[BaseException], None] | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._processor = processor
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._enqueue_timeout = enqueue_timeout
        self._log_path = log_path
        self._on_failure = on_failure
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._failed = threading.Event()
        self._thread = threading.Thread(target=self._consume, name="h3-decoder-queue", daemon=True)
        self._thread.start()

    def _record(self, event: dict[str, Any]) -> None:
        event = {"utc_epoch": time.time(), **event}
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def submit(self, item: dict[str, Any]) -> concurrent.futures.Future[Any]:
        if self._failed.is_set():
            raise RuntimeError("decoder queue stopped after an earlier failure")
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        queued = {"item": item, "future": future}
        try:
            self._queue.put(queued, timeout=self._enqueue_timeout)
        except queue.Full as exc:
            self._record({"state": "enqueue_failed", "seed": item.get("seed"), "reason": "backpressure_timeout"})
            raise TimeoutError("decoder queue remained full until enqueue timeout") from exc
        self._record({"state": "queued", "seed": item.get("seed"), "pending": self._queue.qsize()})
        return future

    def _consume(self) -> None:
        while True:
            try:
                entry = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            if entry is None:
                self._queue.task_done()
                return
            item = entry["item"]
            future = entry["future"]
            self._record({"state": "running", "seed": item.get("seed")})
            try:
                if self._failed.is_set():
                    raise RuntimeError("skipped because an earlier decoder job failed")
                result = self._processor(item)
                future.set_result(result)
                self._record({"state": "success", "seed": item.get("seed")})
            except BaseException as exc:
                future.set_exception(exc)
                self._record({"state": "failure", "seed": item.get("seed"), "error": f"{type(exc).__name__}: {exc}"})
                if not self._failed.is_set():
                    self._failed.set()
                    if self._on_failure:
                        self._on_failure(exc)
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 30) -> None:
        self._closing.set()
        self._thread.join(timeout=max(0, timeout))
        if self._thread.is_alive():
            raise TimeoutError(f"decoder queue did not drain within {timeout:g}s")


def selftest() -> dict[str, Any]:
    from tempfile import TemporaryDirectory

    workflow_checks = []
    long_queue_plan = make_plan("eight_plus_one", 416, 736, 5, 7, 123)
    if long_queue_plan["profile_status"] != "validated" or long_queue_plan["tested_batch_count"] != 6 or not long_queue_plan["long_queue_repeatability_unverified"]:
        raise AssertionError("count>tested_batch_count must remain allowed and carry repeatability labels")
    if make_plan("four_four_plus_one", 736, 1280, 10, 1, 123)["profile_status"] != "unverified":
        raise AssertionError("an unverified profile×mode must remain blocked")
    config_path = PACKAGE_ROOT / "config" / "public-defaults.json"
    if config_path.is_file():
        workflow_config = json.loads(config_path.read_text(encoding="utf-8"))
        for pool_name, expected in (("t10_8", "0,1,2,3,4,5,6,7"), ("t10_4a", "0,1,2,3"), ("t10_4b", "0,1,2,3")):
            pool = workflow_config["pools"][pool_name]
            graph = build_sampler_workflow(workflow_config, pool_name, "offline test prompt", "offline-reference.png", 416, 736, 5, 123, "selftest")
            loader = next(node for node in graph.values() if node.get("class_type") == "H3MultiGPUCLIPLoader")
            initializer = next(node for node in graph.values() if node.get("class_type") == "RayInitializer")
            if loader["inputs"].get("gpu_ids") != expected or int(initializer["inputs"].get("GPU", -1)) != int(pool["gpu_count"]):
                raise AssertionError(f"process-local GPU index invariant failed for {pool_name}")
            workflow_checks.append(pool_name)
        xtx_graph = build_sampler_workflow(workflow_config, "xtx", "offline test prompt", "offline-reference.png", 416, 736, 5, 123, "selftest")
        if "save" not in xtx_graph or xtx_graph["save"].get("class_type") != "SaveH3Latent":
            raise AssertionError("XTX SaveH3Latent output id was not normalized for journal parsing")
        workflow_checks.append("xtx_save_node_normalized")

    lock = threading.Lock()
    jobs: dict[str, dict[str, Any]] = {}
    counts = {"sampler_active": 0, "sampler_peak": 0, "decoder_active": 0, "decoder_peak": 0}
    next_id = 0
    def mock_request(url: str, payload: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
        nonlocal next_id
        if payload is not None:
            mode = payload.get("prompt", {}).get("_mock_mode", "success")
            with lock:
                next_id += 1
                prompt_id = f"mock-{next_id}"
                jobs[prompt_id] = {"mode": mode, "started": time.monotonic(), "done": False}
                key = "sampler" if mode == "sample" else "decoder" if mode == "decode" else None
                if key:
                    counts[f"{key}_active"] += 1
                    counts[f"{key}_peak"] = max(counts[f"{key}_peak"], counts[f"{key}_active"])
            return {"prompt_id": prompt_id}
        prompt_id = url.rsplit("/", 1)[-1]
        with lock:
            item = jobs.get(prompt_id)
            if item is None:
                return {}
            mode = item["mode"]
            if mode == "error_completed":
                return {prompt_id: {"status": {"status_str": "error", "completed": True, "messages": [["execution_error", {"message": "mock error"}]]}}}
            if mode == "timeout" or time.monotonic() - item["started"] < 0.04:
                return {prompt_id: {"status": {"status_str": "running", "completed": False}}}
            if not item["done"]:
                item["done"] = True
                key = "sampler" if mode == "sample" else "decoder" if mode == "decode" else None
                if key:
                    counts[f"{key}_active"] -= 1
            return {prompt_id: {"status": {"status_str": "success", "completed": True}, "outputs": {}}}

    try:
        try:
            submit_and_wait("mock://comfy", {"_mock_mode": "error_completed"}, timeout=1, interval=0.01, request=mock_request)
        except ComfyError:
            pass
        else:
            raise AssertionError("error status must be rejected even when completed is true")

        try:
            submit_and_wait("mock://comfy", {"_mock_mode": "timeout"}, timeout=0.08, interval=0.01, request=mock_request)
        except TimeoutError:
            pass
        else:
            raise AssertionError("pending job must time out")

        with TemporaryDirectory(prefix="h3-pipeline-selftest-") as temporary:
            log = Path(temporary) / "decoder_queue.jsonl"
            decoder = DecodeQueue(
                lambda _job: submit_and_wait("mock://comfy", {"_mock_mode": "decode"}, timeout=2, interval=0.01, request=mock_request),
                max_pending=2,
                enqueue_timeout=1,
                log_path=log,
            )
            futures: list[concurrent.futures.Future[Any]] = []
            def sample_and_enqueue(seed: int) -> None:
                submit_and_wait("mock://comfy", {"_mock_mode": "sample"}, timeout=2, interval=0.01, request=mock_request)
                futures.append(decoder.submit({"seed": seed}))

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                list(executor.map(sample_and_enqueue, (1, 2, 3)))
            decoder.close(timeout=3)
            for future in futures:
                future.result(timeout=1)
            events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if not all(any(event.get("state") == expected for event in events) for expected in ("queued", "running", "success")):
                raise AssertionError("decoder queue success states were not recorded")

        if counts["sampler_peak"] < 2:
            raise AssertionError(f"samplers did not overlap: {counts}")
        if counts["decoder_peak"] != 1:
            raise AssertionError(f"shared decoder was not serialized: {counts}")
        return {
            "status": "passed",
            "checks": ["long_queue_allowed_with_repeatability_label", "unverified_profile_mode_blocked", "workflow_visible_gpu_indices", *workflow_checks, "error_before_completed", "finite_timeout", "parallel_samplers", "enqueue_without_decode_wait", "single_shared_decoder", "queue_jsonl_outcomes"],
            "observed": counts,
        }
    except BaseException:
        raise
