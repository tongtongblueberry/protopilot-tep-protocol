"""Check the handoff between English PDF extraction and the evidence search CLI."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ingest_pdf
import search_cli
from retrieval import Retriever


class ReferenceIngestTests(unittest.TestCase):
    def test_untranslated_reference_searches_but_procedure_requires_korean(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "test.pdf"
            pdf.write_bytes(b"PDF source identity for extractor fixture")
            page = subprocess.CompletedProcess([], 0, stdout="Reference observation from the simulated reactor.")
            with patch("ingest_pdf.subprocess.run", return_value=page):
                rows = ingest_pdf.extract_pages(pdf, pages=[1], doc_id="TEST", revision="1",
                                                uri="urn:example:research", equipment="TEP_REACTOR")
            corpus = Path(directory) / "extracted.json"
            corpus.write_text(json.dumps(rows))
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["search_cli.py", "reactor", "--corpus", str(corpus)]), redirect_stdout(stdout):
                search_cli.main()
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["decision"], "EVIDENCE_ONLY")
            self.assertEqual(result["hits"][0]["id"], rows[0]["id"])
            self.assertEqual(result["hits"][0]["passage"]["ko"], "")
            with self.assertRaises(ValueError):
                Retriever([{**rows[0], "kind": "research_sop"}])


if __name__ == "__main__":
    unittest.main()
