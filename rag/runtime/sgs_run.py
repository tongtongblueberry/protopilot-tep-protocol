#!/usr/bin/env python3
"""Prepare an allowlisted demo bundle; --run creates and cleans up one SGS Pod."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import time
import uuid
import zipfile

import remote_runner


ALLOWED_SUFFIXES = {".py", ".json", ".jsonl", ".csv", ".tsv", ".toml", ".txt", ".md",
                    ".yaml", ".yml", ".html", ".js", ".css", ".lock"}
SECRET_MARKERS = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
                            rb"\b(?:hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
                            rb"sk-[A-Za-z0-9_-]{24,})\b|"
                            rb"(?im:^\s*(?:client-key-data|client-certificate-data|"
                            rb"access-token|refresh-token|id-token)\s*:)" )


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def relative_path(value):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("File paths must be strings of at most 240 characters")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("Use an explicit relative file path: " + value)
    parts = [part.lower() for part in path.parts]
    if any(part.startswith(".") or any(word in part for word in ("secret", "credential", "kubeconfig"))
           for part in parts):
        raise ValueError("Private configuration path is excluded: " + value)
    if path.name.lower() in {"team-source.txt", "token", "id_rsa", "id_ed25519"}:
        raise ValueError("Private team or credential file is excluded: " + value)
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("Unsupported file type, including binary model weights: " + value)
    return path.as_posix()


def prepare(args):
    source = args.source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("--source must name a directory")
    allowed = json.loads(args.allowlist.read_text(encoding="utf-8"))
    if not isinstance(allowed, dict) or not allowed:
        raise ValueError("Allowlist must map relative file paths to code/public/synthetic")
    if len(allowed) > 256:
        raise ValueError("Embedded demo bundle supports at most 256 explicitly listed files")
    command = json.loads(args.command)
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError("--command must be a nonempty JSON array of strings")
    if len(args.result) != len(set(args.result)):
        raise ValueError("Duplicate result paths")
    results = [relative_path(path) for path in args.result]
    if len(results) != len(set(results)):
        raise ValueError("Result paths alias each other")
    plan = {"schema_version": 1, "run_id": uuid.uuid4().hex, "command": command,
            "run_seconds": args.run_seconds, "gpu": args.gpu, "results": results, "files": []}
    payload = io.BytesIO()
    total = 0
    seen = set()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, kind in sorted(allowed.items()):
            if kind not in {"code", "public", "synthetic"}:
                raise ValueError("Every allowlist entry must declare code/public/synthetic")
            relative = relative_path(relative)
            if relative in seen:
                raise ValueError("Allowlist paths alias each other")
            seen.add(relative)
            target = remote_runner.safe_path(source, relative)
            if not target.is_file():
                raise ValueError("Allowlist accepts files only: " + relative)
            if target.stat().st_size > remote_runner.MAX_SOURCE_BYTES - total:
                raise ValueError("Demo source exceeds the 512 KiB embedded payload bound")
            data = target.read_bytes()
            total += len(data)
            if total > remote_runner.MAX_SOURCE_BYTES:
                raise ValueError("Demo source grew beyond the payload bound")
            if SECRET_MARKERS.search(data):
                raise ValueError("Credential-shaped content is excluded: " + relative)
            plan["files"].append({"path": relative, "kind": kind, "bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
            archive.writestr("source/" + relative, data)
        plan_bytes = json.dumps(plan, separators=(",", ":")).encode("utf-8")
        if len(plan_bytes) > 65536:
            raise ValueError("Execution plan exceeds the 64 KiB metadata bound")
        archive.writestr("__sgs_plan__.json", plan_bytes)
    packed = payload.getvalue()
    digest = hashlib.sha256(packed).hexdigest()
    name = "rag-" + plan["run_id"][:20]
    limits = {"cpu": str(args.cpu), "memory": args.memory, "ephemeral-storage": "10Gi"}
    requests = {"cpu": "0", "memory": "0", "ephemeral-storage": "0"}
    if args.gpu:
        limits["nvidia.com/gpu"] = "1"
        requests["nvidia.com/gpu"] = "1"
    bootstrap = Path(remote_runner.__file__).read_text(encoding="utf-8")
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": name, "namespace": args.namespace,
                        "labels": {"app.kubernetes.io/name": "protopilot-rag", "run-id": plan["run_id"]},
                        "annotations": {"payload-sha256": digest}},
           "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": args.run_seconds + 60,
                    "terminationGracePeriodSeconds": 5, "automountServiceAccountToken": False,
                    "nodeSelector": {"kubernetes.io/arch": "amd64", "kubernetes.io/os": "linux"},
                    "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "runAsNonRoot": True,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{"name": "rag", "image": args.image, "imagePullPolicy": "IfNotPresent",
                                    "workingDir": "/tmp", "resources": {"limits": limits, "requests": requests},
                                    "securityContext": {"allowPrivilegeEscalation": False,
                                                        "capabilities": {"drop": ["ALL"]}},
                                    "command": [args.python, "-u", "-c", bootstrap,
                                                base64.b64encode(packed).decode("ascii"), digest]}]}}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "payload.zip").write_bytes(packed)
    write_json(args.output / "pod.json", pod)
    write_json(args.output / "plan.json", {**plan, "payload_sha256": digest, "image": args.image,
                                          "context": args.context, "namespace": args.namespace,
                                          "source_root": str(source), "pod_name": name})
    return pod, plan, digest


class Attempt:
    def __init__(self, args, pod, plan, digest):
        self.args, self.pod, self.plan, self.digest = args, pod, plan, digest
        self.name = pod["metadata"]["name"]
        self.uid = None
        self.base = ["kubectl", "--kubeconfig", str(args.kubeconfig.expanduser()), "--context", args.context,
                     "--namespace", args.namespace, "--request-timeout=15s",
                     "--cache-dir", str(args.output / "kubectl-cache")]
        self.record = {"schema_version": 1, "status": "PREPARED", "run_id": plan["run_id"],
                       "payload_sha256": digest, "commands": [], "cleanup": "NOT_CREATED"}

    def save(self):
        write_json(self.args.output / "attempt.json", self.record)

    def command(self, stage, arguments, timeout=25, raw=False):
        started = time.monotonic()
        completed = subprocess.run(self.base + arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=timeout)
        self.record["commands"].append({"stage": stage, "exit_code": completed.returncode,
                                         "elapsed_s": round(time.monotonic() - started, 6)})
        self.save()
        if len(completed.stdout) > 8 * 1024 * 1024:
            raise RuntimeError("Response exceeds 8 MiB evidence bound")
        if completed.returncode:
            # Authentication helpers may print sensitive data, so keep raw stderr out of artifacts.
            raise RuntimeError(stage + " failed; kubectl exit " + str(completed.returncode))
        return completed.stdout if raw else json.loads(completed.stdout)

    def own(self):
        response = self.command("get_owned", ["get", "pods", "--field-selector=metadata.name=" + self.name,
                                               "-o", "json"])
        items = response.get("items", [])
        if not items:
            return None
        if len(items) != 1:
            raise RuntimeError("Unexpected Pod response")
        pod = items[0]
        meta = pod["metadata"]
        if (meta.get("labels", {}).get("run-id") != self.plan["run_id"] or
                meta.get("annotations", {}).get("payload-sha256") != self.digest or
                meta.get("name") != self.name or meta.get("namespace") != self.args.namespace):
            raise RuntimeError("Pod ownership mismatch")
        if self.uid is not None and meta["uid"] != self.uid:
            raise RuntimeError("Pod UID changed")
        self.uid = meta["uid"]
        self.record["uid"] = self.uid
        self.save()
        return pod

    def capture(self):
        raw = self.command("capture_logs", ["logs", self.name, "--container=rag",
                                             "--limit-bytes=8388608", "--timestamps=false"], raw=True)
        (self.args.output / "pod.log").write_bytes(raw)
        self.record["log_sha256"] = hashlib.sha256(raw).hexdigest()
        markers = [line[len(remote_runner.PREFIX):] for line in raw.decode("utf-8").splitlines()
                   if line.startswith(remote_runner.PREFIX)]
        if len(markers) != 1:
            raise RuntimeError("Missing or duplicate result receipt")
        result = json.loads(markers[0])
        if result.get("run_id") != self.plan["run_id"] or result.get("payload_sha256") != self.digest:
            raise RuntimeError("Result receipt identity mismatch")
        files = result.get("files", [])
        expected = set(self.plan["results"])
        received = [entry["path"] for entry in files]
        if len(received) != len(set(received)) or set(received) - expected:
            raise RuntimeError("Unexpected result paths")
        total = 0
        for entry in files:
            relative = relative_path(entry["path"])
            data = base64.b64decode(entry.pop("base64"), validate=True)
            total += len(data)
            if total > remote_runner.MAX_RESULT_BYTES or len(data) != entry["bytes"]:
                raise RuntimeError("Result size mismatch")
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise RuntimeError("Result checksum mismatch")
            target = remote_runner.safe_path(self.args.output / "returned", relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        write_json(self.args.output / "remote-result.json", result)
        self.record["remote_status"] = result["status"]
        if result["status"] != "SUCCEEDED" or set(received) != expected:
            raise RuntimeError("Remote workload failed or declared outputs are missing")

    def cleanup(self):
        current = self.own()
        if current is None:
            self.record["cleanup"] = "OWNED_UID_ABSENT"
            return
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 5,
                   "preconditions": {"uid": self.uid}}
        write_json(self.args.output / "delete-options.json", options)
        self.command("delete_owned_uid", ["delete", "--raw",
                                           f"/api/v1/namespaces/{self.args.namespace}/pods/{self.name}",
                                           "-f", str(self.args.output / "delete-options.json")])
        for _ in range(10):
            if self.own() is None:
                self.record["cleanup"] = "OWNED_UID_ABSENT"
                return
            time.sleep(1)
        raise RuntimeError("Deletion requested but owned UID is still present")

    def execute(self):
        self.record["started_at_utc"] = datetime.now(timezone.utc).isoformat()
        self.save()
        create_attempted = False
        try:
            # A random run label permits ownership recovery if create's response is lost.
            create_attempted = True
            created = self.command("create_once", ["create", "-f", str(self.args.output / "pod.json"),
                                                    "-o", "json"])
            self.uid = created["metadata"]["uid"]
            self.record["uid"] = self.uid
            write_json(self.args.output / "created-pod.json", created)
            deadline = time.monotonic() + self.args.run_seconds + self.args.start_seconds + 60
            start_deadline = time.monotonic() + self.args.start_seconds
            while time.monotonic() < deadline:
                current = self.own()
                if current is None:
                    raise RuntimeError("Created Pod disappeared")
                status = current.get("status", {})
                self.record["pod_phase"] = status.get("phase")
                self.save()
                if status.get("phase") in {"Succeeded", "Failed"}:
                    write_json(self.args.output / "terminal-pod.json", current)
                    self.capture()
                    if status["phase"] != "Succeeded":
                        raise RuntimeError("Pod did not complete successfully")
                    self.record["status"] = "SUCCEEDED"
                    break
                if status.get("phase") == "Pending" and time.monotonic() >= start_deadline:
                    raise TimeoutError("Pod scheduling/image startup timed out")
                time.sleep(2)
            else:
                raise TimeoutError("Pod execution deadline elapsed")
        except BaseException as exc:
            self.record["status"] = "FAILED"
            self.record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if create_attempted:
                try:
                    self.cleanup()
                except BaseException as exc:
                    self.record["cleanup"] = "PENDING_OR_FAILED"
                    self.record["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
            self.record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            self.save()
        return self.record["status"] == "SUCCEEDED" and self.record["cleanup"] == "OWNED_UID_ABSENT"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True,
                        help='JSON object such as {"app.py":"code","data/demo.json":"synthetic"}')
    parser.add_argument("--command", required=True, help='JSON argv array, e.g. ["python","app.py"]')
    parser.add_argument("--result", action="append", required=True, help="Explicit relative result file, repeatable")
    parser.add_argument("--output", type=Path, required=True, help="New local attempt directory")
    parser.add_argument("--image", required=True, help="Python-equipped public CPU or PyTorch CUDA image")
    parser.add_argument("--python", default="python", help="Python executable in the container")
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--gpu", type=int, choices=(0, 1), default=0)
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--memory", default="8Gi")
    parser.add_argument("--run-seconds", type=int, default=600)
    parser.add_argument("--start-seconds", type=int, default=180)
    parser.add_argument("--run", action="store_true", help="Actually create the Pod using normal kubectl authentication")
    args = parser.parse_args(argv)
    if args.run and args.kubeconfig is None:
        parser.error("--run requires an explicit --kubeconfig")
    if not 1 <= args.run_seconds <= 3600 or not 1 <= args.start_seconds <= 1800:
        parser.error("Run must be 1–3600 seconds and startup must be 1–1800 seconds")
    if not 1 <= args.cpu <= 64 or not re.fullmatch(r"[1-9][0-9]*(?:Mi|Gi)", args.memory):
        parser.error("Specify a positive CPU limit <=64 and memory such as 8Gi")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", args.namespace):
        parser.error("Invalid namespace")
    return args


def main():
    args = parse_args()
    pod, plan, digest = prepare(args)
    print(json.dumps({"prepared": str(args.output.resolve()), "pod": pod["metadata"]["name"],
                      "payload_sha256": digest, "run_requested": args.run}), flush=True)
    if not args.run:
        return 0
    signal.signal(signal.SIGTERM, interrupted)
    return 0 if Attempt(args, pod, plan, digest).execute() else 1


def interrupted(_signal, _frame):
    raise KeyboardInterrupt("SIGTERM")


if __name__ == "__main__":
    raise SystemExit(main())
