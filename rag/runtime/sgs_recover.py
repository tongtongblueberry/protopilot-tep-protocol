"""Collect a finished SGS run after the local client disconnected."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from sgs_run import Attempt, write_json


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_local_results(output, plan, digest):
    receipt = load_json(output / "remote-result.json")
    if (receipt["run_id"] != plan["run_id"] or receipt["payload_sha256"] != digest
            or receipt["status"] != "SUCCEEDED"):
        raise ValueError("No successful receipt for this run")
    entries = receipt["files"]
    if sorted(entry["path"] for entry in entries) != sorted(plan["results"]):
        raise ValueError("Returned paths differ from the submitted plan")
    for entry in entries:
        path = (output / "returned" / entry["path"]).resolve()
        path.relative_to((output / "returned").resolve())
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Returned file differs from the receipt")


def recover(args):
    out = args.output
    plan, spec, state, created = [load_json(out / name) for name in
                                  ("plan.json", "pod.json", "attempt.json", "created-pod.json")]
    digest = hashlib.sha256((out / "payload.zip").read_bytes()).hexdigest()
    uid = created["metadata"]["uid"]
    if (not uid or uid != state.get("uid") or digest != state["payload_sha256"] or digest != plan["payload_sha256"]
            or plan["run_id"] != state["run_id"] or plan["context"] != args.context or plan["namespace"] != args.namespace):
        raise ValueError("Saved run identity differs from the requested workspace")
    attempt = Attempt(args, spec, plan, digest)
    attempt.uid, attempt.record = uid, state
    completed_before = state.get("status") == "SUCCEEDED" and state.get("cleanup") == "OWNED_UID_ABSENT"
    current = attempt.own()
    if current is None:
        check_local_results(out, plan, digest)
        state.update(status="SUCCEEDED", cleanup="OWNED_UID_ABSENT")
    else:
        phase = current["status"]["phase"]
        if phase not in {"Succeeded", "Failed"}:
            raise RuntimeError(f"The owned Pod is {phase}; retry after it finishes")
        write_json(out / "terminal-pod.json", current)
        # Preserve the original client failure until remote state and receipt are verified.
        if not (out / "pre-recovery.json").exists():
            write_json(out / "pre-recovery.json", state)
        try:
            attempt.capture()
        except RuntimeError:
            if phase == "Failed" and (out / "remote-result.json").exists():
                state.update(status="FAILED", pod_phase=phase)
                attempt.cleanup()
                attempt.save()
            raise
        if phase != "Succeeded":
            raise RuntimeError("Terminal Pod and workload receipt disagree")
        check_local_results(out, plan, digest)
        state.update(status="SUCCEEDED", pod_phase=phase)
        attempt.cleanup()
    for key in ("error", "cleanup_error"):
        if key in state:
            state["original_" + key] = state.pop(key)
    now = datetime.now(timezone.utc).isoformat()
    state["recovery_verified_at_utc"] = now
    if not completed_before:
        state["finished_at_utc"] = now
    attempt.save()
    return state["status"], state["cleanup"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Existing attempt directory")
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    print(*recover(parser.parse_args()))
