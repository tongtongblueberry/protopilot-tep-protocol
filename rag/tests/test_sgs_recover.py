import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from sgs_recover import recover
from sgs_run import Attempt


class RecoveryTests(unittest.TestCase):
    def test_failed_owned_pod_without_result_is_removed_and_failure_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            payload = b"synthetic archive"
            digest = hashlib.sha256(payload).hexdigest()
            plan = {"run_id": "demo", "payload_sha256": digest, "context": "demo", "namespace": "demo"}
            state = {**plan, "uid": "test-pod-uid", "status": "FAILED", "cleanup": "PENDING_OR_FAILED"}
            pod = {"metadata": {"name": "demo", "uid": state["uid"]}}
            for name, data in (("plan", plan), ("pod", pod), ("created-pod", pod), ("attempt", state)):
                (output / (name+".json")).write_text(json.dumps(data))
            (output / "payload.zip").write_bytes(payload)
            args = SimpleNamespace(output=output, context="demo", namespace="demo", kubeconfig=Path("demo"))
            def remove_owned(attempt):
                self.assertEqual(attempt.uid, state["uid"])
                attempt.record["cleanup"] = "OWNED_UID_ABSENT"
            with patch.object(Attempt, "own", return_value={**pod, "status": {"phase": "Failed"}}), \
                 patch.object(Attempt, "capture", side_effect=RuntimeError("Missing result receipt")), \
                 patch.object(Attempt, "cleanup", autospec=True, side_effect=remove_owned) as cleanup:
                with self.assertRaisesRegex(RuntimeError, "Missing result"):
                    recover(args)
            cleanup.assert_called_once()
            saved = json.loads((output / "attempt.json").read_text())
            self.assertEqual((saved["status"], saved["pod_phase"], saved["cleanup"]), ("FAILED", "Failed", "OWNED_UID_ABSENT"))
            self.assertEqual(saved["capture_error"]["message"], "Missing result receipt")
