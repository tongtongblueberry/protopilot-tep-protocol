"""Extract auditable reference chunks from selected PDF pages with Poppler."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def extract_pages(pdf, *, pages, doc_id, revision, uri, equipment):
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    rows = []
    for number in pages:
        output = subprocess.run(["pdftotext", "-f", str(number), "-l", str(number),
                                 "-layout", "-enc", "UTF-8", str(pdf), "-"],
                                check=True, capture_output=True, text=True)
        text = output.stdout.strip()
        if not text:
            raise ValueError(f"Page {number} has no extractable text. A checked OCR transcription is required.")
        # Keep numbered steps and headings with their body. Split long unstructured text on paragraph boundaries.
        sections, block = [], []
        for paragraph in re.split(r"\n[ \t]*\n", text):
            if not paragraph.strip():
                continue
            start = paragraph.strip().splitlines()[0]
            new_step = re.match(r"(?i)^(?:step\s+\d+|\d+(?:\.\d+)*[.)]\s+|section\s+[\dA-Z])", start)
            if block and (new_step or sum(map(len, block))+len(paragraph)>1150):
                sections.append("\n\n".join(block))
                block = []
            block.append(paragraph.strip())
        if block:
            sections.append("\n\n".join(block))
        for i, body in enumerate(sections, 1):
            rows.append(dict(id=f"{doc_id}-p{number}-{i}", text=body, ko="", kind="reference",
                             revision=revision, equipment=equipment, mode=[], tags=[],
                             source={"uri":uri, "locator":f"PDF p.{number}, block {i}",
                                     "payload_sha256":digest}))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pages", required=True, help="Comma-separated 1-based page numbers")
    parser.add_argument("--id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--equipment", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pages = list(dict.fromkeys(int(p) for p in args.pages.split(",")))
    if min(pages) < 1:
        parser.error("Page numbers start at 1")
    rows = extract_pages(args.pdf, pages=pages, doc_id=args.id, revision=args.revision,
                         uri=args.source_url, equipment=args.equipment)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2)+"\n")
    print(f"{len(rows)} reference chunks from {len(pages)} page(s)")


if __name__ == "__main__":
    main()
