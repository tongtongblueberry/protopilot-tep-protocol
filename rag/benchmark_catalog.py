"""Evaluate identical public-observation queries across three search configurations."""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

from retrieval import Retriever


def evaluate(rows, probes, model_dir, device):
    summaries = {}
    if device == "cpu":
        import torch
        torch.set_num_threads(4)
    with Retriever(rows, model_dir=model_dir, device=device) as retriever:
        for mode in ("bm25", "hybrid", "hybrid_rerank"):
            retriever.prepare(mode)
            cases = []
            for probe in probes:
                result = retriever.search(probe["query"], mode=mode, filters=probe["filters"],
                                          limit=5, candidate_limit=20)
                ids = [hit["id"] for hit in result["hits"]]
                # The expected card is used only after retrieval.
                expected = set(probe["expected_active_card_clauses"])
                cases.append({"case_id": probe["case_id"], "top_ids": ids,
                              "top1": bool(expected.intersection(ids[:1])),
                              "top5": bool(expected.intersection(ids)),
                              "total_ms": result["timing_ms"]["total"]})
            summaries[mode] = {"case_count": len(cases), "top1": sum(c["top1"] for c in cases),
                               "top5": sum(c["top5"] for c in cases),
                               "median_query_ms": median(c["total_ms"] for c in cases), "cases": cases}
    return summaries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--probes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=Path(__file__).resolve().parent/".models")
    parser.add_argument("--device", choices=("cpu","cuda"), default="cpu")
    args = parser.parse_args()
    data = [path.read_bytes() for path in (args.corpus, args.probes)]
    rows, probes = (json.loads(blob) for blob in data)
    if not rows or not probes:
        parser.error("Both a corpus and public-observation queries are required")
    report = {"corpus_sha256": hashlib.sha256(data[0]).hexdigest(), "probes_sha256": hashlib.sha256(data[1]).hexdigest(),
              "device": args.device, "qdrant_backend": "embedded", "candidate_limit":20, "result_limit":5,
              "row_count":len(rows), "scope":"public scenario replay; expected active card scored after retrieval",
              "modes":evaluate(rows,probes,args.model_dir,args.device)}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    print({m:(x["top1"],x["top5"],x["case_count"]) for m,x in report["modes"].items()})
