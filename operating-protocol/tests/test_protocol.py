import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tep_protocol import ProtocolBook, ProtocolError


def sample_book():
    """Return a small authored fixture, independent of the released incident cards."""
    return {
        "schema_version": 1,
        "sop_id": "TEP-TEST",
        "version": "0.1.0",
        "studio_revision": "852f0c69ab974540750d61bd1dbfafd56bfd80bb",
        "policy": {"max_retrieval_retries": 1, "info_wait_min": 3},
        "sources": {
            "procedure": {"url": "https://example.org/fixture", "locator": "첫 조항"},
            "units": {"url": "https://example.org/units", "locator": "계측 표"},
            "unrelated": {"url": "https://example.org/unused", "locator": "다른 조항"},
        },
        "clauses": [{
            "id": "RC-01",
            "equipment": ["reactor"],
            "mode": 1,
            "control": "closed_loop",
            "title": {"ko": "반응기 관측", "en": "Reactor observation"},
            "terms": {"ko": ["냉각수"], "en": ["cooling water"]},
            "applies_when_ko": "반응기 온도 추세를 확인할 때",
            "required_observations": [{
                "tag": "XMEAS(9)", "unit": "degC", "min_samples": 2,
                "window_min": 3, "source_id": "units",
            }],
            "actions": [{
                "id": "record", "policy": "REQUIRE", "actor": "operator",
                "approval_role": None, "when_ko": "계측값이 유효할 때",
                "instruction_ko": "관측 시각과 값을 기록합니다.",
            }],
            "close": {
                "recovery_ko": "담당자가 관측값을 확인합니다.",
                "review_within_min": 15, "handoff_role": "shift_lead",
            },
            "source_ids": ["procedure"],
        }],
    }


class BookTests(unittest.TestCase):
    def test_lookup_preserves_clauses_and_resolves_observation_sources(self):
        source = sample_book()
        book = ProtocolBook(source)
        record = book.show("RC-01")
        self.assertEqual(record, book.show("TEP-TEST@0.1.0/RC-01"))
        self.assertEqual(record, next(book.records()))
        for field, value in source["clauses"][0].items():
            self.assertEqual(record[field], value, field)
        self.assertEqual(set(record["sources"]), {"procedure", "units"})
        self.assertEqual(record["policy"], source["policy"])
        self.assertIn("관측 시각과 값을 기록합니다.", record["text_ko"])
        self.assertIn("cooling water", record["text_en"])

        record["actions"][0]["actor"] = "changed"
        source["clauses"][0]["mode"] = 2
        self.assertEqual(book.show("RC-01")["actions"][0]["actor"], "operator")
        self.assertEqual(book.show("RC-01")["mode"], 1)

    def test_rejects_lookup_using_a_different_version(self):
        with self.assertRaisesRegex(ProtocolError, "unknown clause"):
            ProtocolBook(sample_book()).show("TEP-TEST@0.0.9/RC-01")

    def test_rejects_duplicate_clause_and_action_ids(self):
        document = sample_book()
        document["clauses"].append(document["clauses"][0])
        with self.assertRaisesRegex(ProtocolError, "duplicate clause id"):
            ProtocolBook(document)

        document = sample_book()
        actions = document["clauses"][0]["actions"]
        actions.append(actions[0])
        with self.assertRaisesRegex(ProtocolError, "actions has duplicate id"):
            ProtocolBook(document)

    def test_rejects_unknown_source_in_observations_and_clause(self):
        for field in ("required_observations", "source_ids"):
            with self.subTest(field=field):
                document = sample_book()
                clause = document["clauses"][0]
                if field == "required_observations":
                    clause[field][0]["source_id"] = "missing"
                else:
                    clause[field] = ["missing"]
                with self.assertRaisesRegex(ProtocolError, "unknown source 'missing'"):
                    ProtocolBook(document)

    def test_clause_reference_must_resolve_in_the_same_book(self):
        document = sample_book()
        document["clauses"][0]["close"]["recovery_ko"] = "RC-02로 진행합니다."
        with self.assertRaisesRegex(ProtocolError, "unknown clause 'RC-02'"):
            ProtocolBook(document)

        next_clause = sample_book()["clauses"][0]
        next_clause["id"] = "RC-02"
        document["clauses"].append(next_clause)
        self.assertEqual(len(ProtocolBook(document)), 2)

    def test_accepts_pinned_observations_and_preserves_unit_labels(self):
        document = sample_book()
        clause = document["clauses"][0]
        template = clause["required_observations"][0]
        clause["required_observations"] = [
            {**template, "tag": "XMEAS(7)", "unit": "kPa(g)"},
            {**template, "tag": "XMEAS(41)", "unit": "mol %"},
            {**template, "tag": "XMEASADD(32)", "unit": "mol%"},
        ]
        book = ProtocolBook(document)
        self.assertEqual(book.show("RC-01")["required_observations"], clause["required_observations"])

    def test_rejects_wrong_units_and_bool_as_a_sample_count(self):
        for field, value in (("unit", "kg/h"), ("min_samples", True)):
            with self.subTest(field=field):
                document = sample_book()
                document["clauses"][0]["required_observations"][0][field] = value
                with self.assertRaisesRegex(ProtocolError, field):
                    ProtocolBook(document)

    def test_requires_action_permissions_and_closure(self):
        for field in ("policy", "approval_role", "handoff_role", "review_within_min"):
            with self.subTest(field=field):
                document = sample_book()
                clause = document["clauses"][0]
                if field in ("policy", "approval_role"):
                    del clause["actions"][0][field]
                else:
                    del clause["close"][field]
                with self.assertRaisesRegex(ProtocolError, field):
                    ProtocolBook(document)


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "protocol.json"
        self.path.write_text(json.dumps(sample_book(), ensure_ascii=False), encoding="utf-8")

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tep_protocol", "--path", str(self.path), *args],
            capture_output=True, check=False,
            env={**os.environ, "PYTHONIOENCODING": "ascii"},
        )

    def test_check_show_and_export_agree_and_export_utf8(self):
        checked = self.cli("check")
        shown = self.cli("show", "TEP-TEST@0.1.0/RC-01")
        exported = self.cli("export")
        for outcome in (checked, shown, exported):
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
            self.assertEqual(outcome.stderr, b"")
        self.assertIn(b"1 clauses validated", checked.stdout)
        self.assertEqual(exported.stdout.count(b"\n"), 1)
        self.assertIn("반응기 관측".encode("utf-8"), exported.stdout)
        self.assertEqual(json.loads(shown.stdout), json.loads(exported.stdout))

    def test_cli_rejects_unsupported_observation_tags_and_revision(self):
        for field, value in (
            ("tag", "XMEAS(90)"), ("tag", "XMEASADD(33)"),
            ("tag", "XMEASMONITOR(1)"), ("studio_revision", "0" * 40),
        ):
            with self.subTest(field=field, value=value):
                document = sample_book()
                if field == "tag":
                    document["clauses"][0]["required_observations"][0][field] = value
                else:
                    document[field] = value
                self.path.write_text(json.dumps(document), encoding="utf-8")
                checked = self.cli("check")
                self.assertEqual(checked.returncode, 1, checked.stderr)
                self.assertEqual(checked.stdout, b"")
                self.assertIn(field.encode(), checked.stderr)

    def test_cli_resolves_references_and_ignores_unrelated_text(self):
        document = sample_book()
        clause = document["clauses"][0]
        note = "계기 XMEAS(9)와 카드 RC-31-SIM, https://example.org/RC-90, RC-91@example.org를 기록합니다."
        instruction = "RC-01을 점검한 뒤 TEP-TEST@0.1.0/RC-01로 돌아갑니다. " + note
        clause["actions"][0]["instruction_ko"] = instruction
        self.path.write_text(json.dumps(document), encoding="utf-8")
        checked = self.cli("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        record = json.loads(self.cli("show", "TEP-TEST@0.1.0/RC-01").stdout)
        self.assertEqual(record["actions"][0]["instruction_ko"], instruction)

        for ref in ("TEP-TEST@0.0.9/RC-01", "OTHER-SOP@0.1.0/RC-01", "TEP-TEST@0.1.0/MISSING"):
            with self.subTest(reference=ref):
                clause["close"]["recovery_ko"] = f"복귀할 때는 `{ref}`로 돌아갑니다."
                self.path.write_text(json.dumps(document), encoding="utf-8")
                for command in ("check", "export"):
                    result = self.cli(command)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertIn(ref.encode("ascii"), result.stderr)

    def test_invalid_books_never_emit_a_partial_export(self):
        valid = sample_book()
        valid["clauses"].append({"id": "RC-02"})
        for content in (
            "{",
            '{"schema_version": 1, "schema_version": 1}',
            '{"info_wait_min": NaN}',
            json.dumps(valid),
        ):
            with self.subTest(content=content[:50]):
                self.path.write_text(content, encoding="utf-8")
                outcome = self.cli("export")
                self.assertEqual(outcome.returncode, 1, outcome.stderr)
                self.assertEqual(outcome.stdout, b"")
                self.assertIn(b"error:", outcome.stderr)


if __name__ == "__main__":
    unittest.main()
