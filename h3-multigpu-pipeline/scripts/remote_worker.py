#!/usr/bin/env python3
"""Remote helper source; pipeline.py streams this code to the configured SSH host."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def http_json(url, payload=None, timeout=15):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fail_if_error(status, prompt_id):
    if str(status.get("status_str", "")).lower() == "error":
        raise RuntimeError(f"job {prompt_id} failed: {json.dumps(status, ensure_ascii=False)}")
    for message in status.get("messages", []) or []:
        if isinstance(message, (list, tuple)) and message and message[0] == "execution_error":
            detail = message[1] if len(message) > 1 else message
            raise RuntimeError(f"job {prompt_id} execution_error: {json.dumps(detail, ensure_ascii=False)}")


def write_journal(path, value):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def media_output(outputs, save_node, output_root):
    if not isinstance(save_node, dict):
        raise RuntimeError("SaveVideo output specification must be an object")
    if save_node.get("class_type", "SaveVideo") != "SaveVideo":
        raise RuntimeError("decode output must name a SaveVideo node")
    # The packaged decoder workflow fixes its SaveVideo node id to `save`.
    # An adapter may override the id, but never the node class/output key.
    node_id = str(save_node.get("node_id", "save"))
    node_output = outputs.get(node_id)
    candidates = node_output.get("images", []) if isinstance(node_output, dict) else []
    if not isinstance(candidates, list):
        raise RuntimeError("SaveVideo history output 'images' must be a list")
    candidates = [
        value for value in candidates
        if isinstance(value, dict) and str(value.get("filename", "")).lower().endswith(".mp4")
    ]
    if not candidates:
        raise RuntimeError("completed decode history has no MP4 output record")
    item = candidates[-1]
    root = Path(output_root).resolve()
    path = (root / item.get("subfolder", "") / item["filename"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise RuntimeError("reported MP4 is missing or outside the configured output root")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=180, check=True,
    )
    metadata = json.loads(probe.stdout)
    streams = metadata.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    expected = save_node.get("expected_media", {})
    if not video or not audio:
        raise RuntimeError("MP4 must contain both video and audio streams")
    observed = {
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "frames": int(video.get("nb_read_frames", video.get("nb_frames", 0))),
        "fps": video.get("avg_frame_rate"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": int(audio.get("sample_rate", 0)),
        "audio_channels": int(audio.get("channels", 0)),
    }
    for key in ("width", "height", "frames"):
        if expected.get(key) is not None and observed[key] != int(expected[key]):
            raise RuntimeError(f"MP4 {key} mismatch: expected {expected[key]}, got {observed[key]}")
    if observed["fps"] != "24/1":
        raise RuntimeError(f"MP4 fps mismatch: {observed['fps']}")
    if observed["video_codec"] != "h264" or observed["audio_codec"] != "aac":
        raise RuntimeError("MP4 codec mismatch; expected H.264 video and AAC audio")
    if observed["audio_sample_rate"] != 32000 or observed["audio_channels"] != 2:
        raise RuntimeError("MP4 audio mismatch; expected AAC 32kHz stereo")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=save_node.get("ffmpeg_timeout", 600), check=True,
    )
    return {"file": path.name, "bytes": path.stat().st_size, **observed, "full_cpu_decode": True}


def submit(spec):
    api = spec["api"]
    graph = spec["graph"]
    timeout = spec["timeout"]
    interval = spec.get("poll_interval", 1)
    started = time.monotonic()
    result = http_json(api.rstrip("/") + "/prompt", {"prompt": graph})
    if result.get("node_errors"):
        raise RuntimeError(f"node_errors: {json.dumps(result['node_errors'], ensure_ascii=False)}")
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError("ComfyUI returned no prompt_id")
    journal = {
        "schema": "h3-pipeline-remote-job-v1",
        "run_id": spec["run_id"],
        "seed": spec["seed"],
        "kind": spec["kind"],
        "prompt_id": prompt_id,
        "state": "submitted",
        "submitted_epoch": time.time(),
    }
    write_journal(spec.get("journal_path"), journal)
    print(json.dumps({"event": "submitted", "prompt_id": prompt_id}), flush=True)
    deadline = started + timeout
    while time.monotonic() < deadline:
        item = http_json(
            api.rstrip("/") + "/history/" + str(prompt_id),
            timeout=min(15, max(0.1, deadline - time.monotonic())),
        ).get(str(prompt_id))
        if item:
            status = item.get("status", {}) or {}
            fail_if_error(status, prompt_id)
            if status.get("completed"):
                elapsed = round(time.monotonic() - started, 3)
                response = {"prompt_id": prompt_id, "elapsed_seconds": elapsed, "status": "success"}
                output_node = spec.get("output_node")
                if output_node is not None:
                    output_root = Path(spec["output_root"]).resolve()
                    outputs = item.get("outputs", {}).get(str(output_node), {})
                    latents = outputs.get("latents", [])
                    if len(latents) != 1:
                        raise RuntimeError("SaveH3Latent output did not contain exactly one latent")
                    ref = latents[0]
                    root = Path(spec["output_root"]).resolve()
                    latent = (root / ref.get("subfolder", "") / ref["filename"]).resolve()
                    if not latent.is_relative_to(root):
                        raise RuntimeError("latent path escaped configured output root")
                    sidecar = Path(str(latent) + ".sha256")
                    if not latent.is_file() or not sidecar.is_file():
                        raise RuntimeError("latent or SHA-256 sidecar is missing")
                    expected = sidecar.read_text(encoding="ascii").split()[0].lower()
                    digest = hashlib.sha256()
                    with latent.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != expected:
                        raise RuntimeError("saved latent SHA-256 does not match its sidecar")
                    response["latent"] = {
                        "path": str(latent),
                        "sha256": expected,
                        "bytes": latent.stat().st_size,
                        "filename": latent.name,
                    }
                    journal.update({"state": "latent_saved", "latent": response["latent"], "completed_epoch": time.time()})
                else:
                    outputs = item.get("outputs", {})
                    response["outputs"] = outputs
                    response["media"] = media_output(outputs, spec["save_node"], spec["output_root"])
                    journal.update({"state": "decode_success", "media": response["media"], "outputs": outputs, "completed_epoch": time.time()})
                write_journal(spec.get("journal_path"), journal)
                return response
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"job {prompt_id} exceeded {timeout:g}s")


def job_status(spec):
    journal_path = Path(spec["journal_path"])
    if not journal_path.is_file():
        return {"state": "unknown", "reason": "remote_job_journal_missing"}
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    prompt_id = str(journal["prompt_id"])
    item = http_json(spec["api"].rstrip("/") + "/history/" + prompt_id, timeout=spec.get("http_timeout", 15)).get(prompt_id)
    if not item:
        return {"state": "unknown", "prompt_id": prompt_id, "reason": "history_entry_missing"}
    status = item.get("status", {}) or {}
    fail_if_error(status, prompt_id)
    if not status.get("completed"):
        return {"state": "running", "prompt_id": prompt_id}
    outputs = item.get("outputs", {})
    if journal.get("kind") == "sampler":
        save_node = spec["output_node"]
        latents = outputs.get(str(save_node), {}).get("latents", [])
        if len(latents) != 1:
            return {"state": "unknown", "prompt_id": prompt_id, "reason": "latent_output_not_found"}
        ref = latents[0]
        root = Path(spec["output_root"]).resolve()
        latent = (root / ref.get("subfolder", "") / ref["filename"]).resolve()
        sidecar = Path(str(latent) + ".sha256")
        if not latent.is_relative_to(root) or not latent.is_file() or not sidecar.is_file():
            return {"state": "unknown", "prompt_id": prompt_id, "reason": "latent_not_saved"}
        expected = sidecar.read_text(encoding="ascii").split()[0].lower()
        digest = hashlib.sha256(latent.read_bytes()).hexdigest()
        if digest != expected:
            return {"state": "unknown", "prompt_id": prompt_id, "reason": "latent_hash_mismatch"}
        result = {"state": "latent_saved", "prompt_id": prompt_id, "latent": {"path": str(latent), "sha256": expected, "bytes": latent.stat().st_size, "filename": latent.name}}
        journal.update(result)
        write_journal(journal_path, journal)
        return result
    media = media_output(outputs, spec["save_node"], spec["output_root"])
    result = {"state": "decode_success", "prompt_id": prompt_id, "outputs": outputs, "media": media}
    journal.update(result)
    write_journal(journal_path, journal)
    return result


def resume_existing(spec):
    """Wait for an already-submitted prompt; this path never posts /prompt."""
    deadline = time.monotonic() + float(spec["timeout"])
    while time.monotonic() < deadline:
        status = job_status(spec)
        if status.get("state") in ("latent_saved", "decode_success"):
            return status
        if status.get("state") == "unknown":
            raise RuntimeError("cannot safely resume submitted job: " + status.get("reason", "unknown state"))
        time.sleep(min(float(spec.get("poll_interval", 1)), max(0, deadline - time.monotonic())))
    raise TimeoutError(f"existing prompt exceeded {spec['timeout']:g}s; journal retained for later resume")


def preflight(spec):
    api = spec["api"].rstrip("/")
    http_json(api + "/system_stats", timeout=spec.get("http_timeout", 15))
    nodes = http_json(api + "/object_info", timeout=spec.get("http_timeout", 15))
    missing = sorted(set(spec["required_nodes"]) - set(nodes))
    if missing:
        raise RuntimeError("required ComfyUI nodes missing: " + ", ".join(missing))
    missing_models = []
    for requirement in spec.get("required_models", []):
        class_type = requirement["class_type"]
        input_name = requirement["input"]
        expected = requirement["name"]
        node = nodes.get(class_type, {})
        input_spec = node.get("input", {}).get("required", {}).get(input_name)
        choices = input_spec[0] if isinstance(input_spec, (list, tuple)) and input_spec else None
        if isinstance(choices, list) and expected not in choices:
            missing_models.append({"class_type": class_type, "input": input_name, "name": expected})
    if missing_models:
        raise RuntimeError("required model entries missing from ComfyUI object_info: " + json.dumps(missing_models, ensure_ascii=False))
    return {"status": "pass", "api": api, "required_nodes": spec["required_nodes"], "missing_nodes": [], "model_requirements_checked": len(spec.get("required_models", [])), "missing_models": []}


def upload_reference(spec):
    raw = base64.b64decode(spec["data_b64"], validate=True)
    filename = Path(spec["filename"]).name
    boundary = "----h3" + secrets.token_hex(16)
    subfolder = "h3_pipeline/" + spec["run_id"]
    body = bytearray()
    fields = {"subfolder": subfolder, "type": "input", "overwrite": "false"}
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    body.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
    )
    body.extend(raw)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        spec["api"].rstrip("/") + "/upload/image",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=spec.get("http_timeout", 15)) as response:
        result = json.load(response)
    name = result.get("name")
    folder = result.get("subfolder") or subfolder
    if not name:
        raise RuntimeError("ComfyUI upload response did not contain an image name")
    return {"image": f"{folder}/{name}", "uploaded": True}


def verify_owner(pid, token, expected_executable):
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    marker = ("H3_PIPELINE_INSTANCE=" + token).encode()
    if marker not in environ:
        return False
    joined = b" ".join(part for part in argv if part)
    return expected_executable.encode() in joined


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def service(spec):
    action = spec["action"]
    state_path = Path(spec["state_path"])
    if action == "status":
        if not state_path.is_file():
            return {"status": "not_running", "label": spec["label"]}
        saved = json.loads(state_path.read_text())
        if verify_owner(saved["pid"], saved["token"], saved["expected_executable"]):
            return {"status": "running", "label": spec["label"], "pid": saved["pid"], "port": spec["port"]}
        return {"status": "ownership_mismatch", "label": spec["label"], "pid": saved.get("pid")}
    if action == "start":
        if state_path.exists():
            saved = json.loads(state_path.read_text())
            if verify_owner(saved["pid"], saved["token"], saved["expected_executable"]):
                return {"status": "already_running", "label": spec["label"], "pid": saved["pid"]}
            if pid_alive(saved["pid"]):
                raise RuntimeError("service state PID is alive but ownership token/cmdline mismatches; refusing to overwrite")
            archive = state_path.with_name(f"{state_path.name}.stale-{saved['pid']}-{time.time_ns()}")
            os.replace(state_path, archive)
        try:
            http_json(f"http://{spec['api_host']}:{spec['port']}/system_stats", timeout=1)
        except Exception:
            pass
        else:
            raise RuntimeError("port already serves ComfyUI but is not owned by this package; refusing to adopt it")
        for directory in spec.get("directories", []):
            Path(directory).mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(24)
        env = os.environ.copy()
        for key in spec.get("unset", []):
            env.pop(key, None)
        env.update(spec.get("environment", {}))
        env["H3_PIPELINE_INSTANCE"] = token
        log_path = Path(spec["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as log:
            child = subprocess.Popen(
                spec["argv"], cwd=spec["cwd"], env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {"pid": child.pid, "token": token, "expected_executable": spec["expected_executable"], "label": spec["label"]}
        temporary = state_path.with_suffix(state_path.suffix + ".part")
        temporary.write_text(json.dumps(state) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_path)
        deadline = time.monotonic() + spec["start_timeout"]
        while time.monotonic() < deadline:
            if not verify_owner(child.pid, token, spec["expected_executable"]):
                raise RuntimeError("service process exited or ownership verification failed during startup")
            try:
                http_json(f"http://{spec['api_host']}:{spec['port']}/system_stats", timeout=1)
                return {"status": "started", "label": spec["label"], "pid": child.pid, "port": spec["port"]}
            except Exception:
                time.sleep(1)
        raise TimeoutError(f"service {spec['label']} did not become ready; owned PID left for inspection")
    if action == "stop":
        if not state_path.is_file():
            return {"status": "not_running", "label": spec["label"]}
        saved = json.loads(state_path.read_text())
        pid = int(saved["pid"])
        if not verify_owner(pid, saved["token"], saved["expected_executable"]):
            if not pid_alive(pid):
                state_path.unlink(missing_ok=True)
                return {"status": "stale_state_removed", "label": spec["label"], "pid": pid}
            raise RuntimeError("service PID ownership cannot be proven; refusing to signal it")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + spec["stop_timeout"]
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                current = json.loads(state_path.read_text()) if state_path.is_file() else None
                if current and current.get("token") == saved["token"] and int(current.get("pid", -1)) == pid:
                    state_path.unlink(missing_ok=True)
                    return {"status": "stopped", "label": spec["label"], "pid": pid, "state_removed": True}
                raise RuntimeError("service exited but state changed concurrently; refusing to clear another owner state")
            if not verify_owner(pid, saved["token"], saved["expected_executable"]):
                raise RuntimeError("PID remains alive but ownership changed after SIGTERM; refusing to clear state")
            time.sleep(0.5)
        raise TimeoutError("owned service did not stop after SIGTERM; no SIGKILL was sent")
    raise ValueError("unknown service action")


def receive_latent(input_root, relative_path, expected_sha, input_stream=None):
    root = Path(input_root).resolve()
    destination = (root / relative_path).resolve()
    if not destination.is_relative_to(root) or destination == root:
        raise RuntimeError("destination escaped decoder input root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    already_present = False
    if destination.exists():
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        sidecar = Path(str(destination) + ".sha256")
        if digest == expected_sha.lower() and sidecar.is_file() and sidecar.read_text(encoding="ascii").split()[0].lower() == digest:
            already_present = True
        else:
            raise RuntimeError("decoder destination exists with different or incomplete content; refusing overwrite")
    if already_present:
        incoming = hashlib.sha256()
        stream = input_stream or sys.stdin.buffer
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            incoming.update(chunk)
        if incoming.hexdigest() != expected_sha.lower():
            raise RuntimeError("replayed latent stream SHA-256 mismatch for existing destination")
        return {"status": "already_present", "relative_path": relative_path, "sha256": digest}
    temporary = destination.with_name(destination.name + ".part-" + secrets.token_hex(6))
    digest = hashlib.sha256()
    try:
        with temporary.open("xb") as output:
            while True:
                stream = input_stream or sys.stdin.buffer
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_sha.lower():
            raise RuntimeError("transferred latent SHA-256 mismatch")
        os.replace(temporary, destination)
        sidecar = Path(str(destination) + ".sha256")
        side_part = Path(str(sidecar) + ".part-" + secrets.token_hex(4))
        side_part.write_text(f"{expected_sha.lower()}  {destination.name}\n", encoding="ascii")
        os.replace(side_part, sidecar)
        return {"status": "transferred", "relative_path": relative_path, "sha256": expected_sha.lower()}
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def handoff_source(spec):
    source = Path(spec["source_path"])
    sidecar = Path(str(source) + ".sha256")
    expected = sidecar.read_text(encoding="ascii").split()[0].lower()
    if expected != spec["sha256"].lower():
        raise RuntimeError("source sidecar changed since sampler completion")
    receiver_code = base64.b64decode(spec["receiver_code_b64"], validate=True).decode("utf-8")
    command = "python3 -c " + shlex.quote(receiver_code) + " receive " + " ".join(
        shlex.quote(value) for value in (spec["decoder_input_root"], spec["relative_path"], expected)
    )
    ssh = ["ssh", "-T", *spec.get("handoff_ssh_options", []), "--", spec["decoder_target"], command]
    child = subprocess.Popen(ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    digest = hashlib.sha256()
    timer = threading.Timer(float(spec.get("child_timeout", 900)), child.kill)
    timer.daemon = True
    timer.start()
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                child.stdin.write(chunk)
        child.stdin.close()
        stdout = child.stdout.read()
        stderr = child.stderr.read()
        code = child.wait(timeout=max(1, float(spec.get("child_timeout", 900))))
    except BaseException:
        if child.poll() is None:
            child.kill()
        child.wait()
        raise
    finally:
        timer.cancel()
    if code != 0:
        raise RuntimeError(f"source-to-decoder SSH handoff failed: {stderr.decode(errors='replace')[-2000:]}")
    if digest.hexdigest() != expected:
        raise RuntimeError("source latent SHA-256 mismatch during source-to-decoder handoff")
    try:
        result = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("decoder receiver returned no valid JSON handoff receipt") from exc
    if not isinstance(result, dict):
        raise RuntimeError("decoder receiver handoff receipt must be a JSON object")
    if result.get("relative_path") != spec["relative_path"] or result.get("sha256", "").lower() != expected:
        raise RuntimeError("decoder receiver receipt path/hash does not match the requested latent")
    if result.get("status") not in ("transferred", "already_present"):
        raise RuntimeError(f"decoder receiver did not confirm transfer: {result!r}")
    return {**result, "stdout_bytes": len(stdout)}


def handoff_local(spec):
    source = Path(spec["source_path"])
    sidecar = Path(str(source) + ".sha256")
    expected = sidecar.read_text(encoding="ascii").split()[0].lower()
    if expected != spec["sha256"].lower():
        raise RuntimeError("source sidecar changed since sampler completion")
    with source.open("rb") as stream:
        return receive_latent(spec["decoder_input_root"], spec["relative_path"], expected, stream)


def main():
    mode = sys.argv[1]
    if mode == "receive":
        result = receive_latent(sys.argv[2], sys.argv[3], sys.argv[4])
        print(json.dumps(result, ensure_ascii=False))
        return
    spec = json.load(sys.stdin)
    if mode == "service":
        result = service(spec)
    elif mode == "preflight":
        result = preflight(spec)
    elif mode == "upload-reference":
        result = upload_reference(spec)
    elif mode in ("submit-sampler", "submit-decode"):
        result = submit(spec)
    elif mode == "job-status":
        result = job_status(spec)
    elif mode == "resume-job":
        result = resume_existing(spec)
    elif mode == "handoff-source":
        result = handoff_source(spec)
    elif mode == "handoff-local":
        result = handoff_local(spec)
    else:
        raise ValueError("unknown remote worker mode")
    print(json.dumps({"event": "result", "data": result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
