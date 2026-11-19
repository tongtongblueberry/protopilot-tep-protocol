"""Small stdlib runner embedded in the one-Pod manifest by sgs_run.py."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import zipfile


PREFIX = "SGS_RAG_RESULT "
MAX_SOURCE_BYTES = 512 * 1024
MAX_RESULT_BYTES = 1024 * 1024
LOG_TAIL_BYTES = 32 * 1024


def safe_path(root, relative):
    parts = PurePosixPath(relative)
    if not parts.parts or parts.is_absolute() or ".." in parts.parts or "\\" in relative:
        raise ValueError("Invalid relative path")
    target = root.joinpath(*parts.parts)
    if any(parent.is_symlink() for parent in [target, *target.parents] if parent != root.parent):
        raise ValueError("Symlink is not an allowed input or result")
    target.resolve().relative_to(root.resolve())
    return target


def unpack(payload, expected_sha, root):
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        raise ValueError("Payload SHA-256 mismatch")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        if len({member.filename for member in members}) != len(members):
            raise ValueError("Duplicate archive members")
        if sum(member.file_size for member in members) > MAX_SOURCE_BYTES + 65536:
            raise ValueError("Payload exceeds the expanded size limit")
        plan = json.loads(archive.read("__sgs_plan__.json"))
        expected_names = {"__sgs_plan__.json", *["source/" + entry["path"] for entry in plan["files"]]}
        if set(archive.namelist()) != expected_names:
            raise ValueError("Archive differs from allowlist")
        for entry in plan["files"]:
            data = archive.read("source/" + entry["path"])
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("Input file digest mismatch")
            target = safe_path(root, entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return plan


def run_command(command, root, timeout):
    environment = dict(os.environ)
    environment.update({"HF_HOME": str(root / ".hf-cache"), "PIP_NO_CACHE_DIR": "1",
                        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    started = time.monotonic()
    proc = subprocess.Popen(command, cwd=root, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    tail = bytearray()
    digest = hashlib.sha256()
    output_bytes = 0
    timed_out = False
    reader = selectors.DefaultSelector()
    reader.register(proc.stdout, selectors.EVENT_READ)
    try:
        while reader.get_map():
            if time.monotonic() - started >= timeout:
                timed_out = True
                break
            for key, _ in reader.select(timeout=0.25):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    reader.unregister(key.fileobj)
                    continue
                output_bytes += len(chunk)
                digest.update(chunk)
                tail.extend(chunk)
                del tail[:-LOG_TAIL_BYTES]
        if not timed_out:
            proc.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        reader.close()
        # Also reap descendants holding stdout open after their parent exits.
        if timed_out or proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)
        proc.stdout.close()
    return {"exit_code": proc.returncode, "timed_out": timed_out,
            "elapsed_s": round(time.monotonic() - started, 6),
            "captured_stdout_bytes": output_bytes, "captured_stdout_sha256": digest.hexdigest(),
            "stdout_capture_complete": not timed_out,
            "stdout_tail": tail.decode("utf-8", errors="replace")}


def collect_results(root, paths):
    collected = []
    total = 0
    for relative in paths:
        target = safe_path(root, relative)
        if not target.is_file():
            raise ValueError("Declared result is missing: " + relative)
        size = target.stat().st_size
        total += size
        if total > MAX_RESULT_BYTES:
            raise ValueError("Declared results exceed the 1 MiB transfer bound")
        data = target.read_bytes()
        if len(data) != size:
            raise ValueError("Result changed during capture")
        collected.append({"path": relative, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(),
                          "base64": base64.b64encode(data).decode("ascii")})
    return collected


def main():
    started = time.monotonic()
    result = {"schema_version": 1, "status": "FAILED", "files": [],
              "probe": {"python": sys.version, "platform": platform.platform(),
                        "machine": platform.machine()}}
    try:
        payload = base64.b64decode(sys.argv[1], validate=True)
        result["payload_sha256"] = sys.argv[2]
        root = Path(tempfile.mkdtemp(prefix="sgs-rag-"))
        plan = unpack(payload, sys.argv[2], root)
        result["run_id"] = plan["run_id"]
        if plan["gpu"]:
            probe = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,uuid",
                                    "--format=csv,noheader"], capture_output=True, timeout=10)
            result["probe"]["gpu_exit_code"] = probe.returncode
            result["probe"]["gpu"] = probe.stdout.decode("utf-8", errors="replace")[:4096]
            if probe.returncode:
                raise RuntimeError("Allocated GPU probe failed")
        result["execution"] = run_command(plan["command"], root, plan["run_seconds"])
        result["files"] = collect_results(root, plan["results"])
        if result["execution"]["exit_code"] == 0 and not result["execution"]["timed_out"]:
            result["status"] = "SUCCEEDED"
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    result["elapsed_s"] = round(time.monotonic() - started, 6)
    print(PREFIX + json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
    return 0 if result["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
