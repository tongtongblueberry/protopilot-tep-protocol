"""Run the three retrieval modes against a fixed, labeled JSON query set."""

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import tempfile

from retrieval import (
    BATCH_SIZE, BGE_MAX_TOKENS, BM25_B, BM25_K1, MODEL_FILES, MODES,
    RERANK_INSTRUCTION, RERANK_MAX_TOKENS, RERANK_PREFIX, RERANK_SUFFIX,
    RRF_K, Retriever,
)


BASE = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=BASE / "data/corpus.json")
    parser.add_argument("--queries", type=Path, default=BASE / "data/pilot_queries.json")
    parser.add_argument("--model-dir", type=Path, default=BASE / ".models")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--candidate-limit", type=int, default=9)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True, help="New JSON result file")
    args = parser.parse_args(argv)
    plan_path = args.output.with_suffix(".plan.json")
    if args.output.exists() or plan_path.exists():
        parser.error("Choose a new output path to preserve earlier measurements")
    rows = json.loads(args.corpus.read_text())
    cases = json.loads(args.queries.read_text())
    corpus_ids = {row["id"] for row in rows}
    if not cases or len({case["id"] for case in cases}) != len(cases):
        parser.error("Queries must have unique IDs and at least one case")
    for case in cases:
        if not isinstance(case["query"], str) or not case["query"].strip():
            parser.error("Each case needs a nonempty query")
        if not case["relevant"] or set(case["relevant"]) - corpus_ids:
            parser.error("Each case needs relevant IDs present in the corpus")
    if args.limit < 1 or args.candidate_limit < args.limit:
        parser.error("Positive limits must satisfy candidate-limit >= limit")
    pins = {"corpus": sha256(args.corpus), "queries": sha256(args.queries),
            "retrieval.py": sha256(BASE / "retrieval.py"), "benchmark.py": sha256(Path(__file__))}
    plan = {
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(), "input_sha256": pins,
        "corpus_rows": len(rows), "cases": cases, "modes": args.modes,
        "candidate_limit": args.candidate_limit, "result_limit": args.limit, "filters": None,
        "settings": {
            "bm25": {"k1": BM25_K1, "b": BM25_B,
                      "idf": "log(1 + (N - df + 0.5)/(df + 0.5))", "query_weight": "term count",
                      "tokenization": "casefold + Unicode word unigrams; no stemming or stopwords"},
            "rrf_k": RRF_K, "bge_pooling": "L2-normalized CLS", "dtype": "float32",
            "bge_max_tokens": BGE_MAX_TOKENS, "reranker_max_tokens": RERANK_MAX_TOKENS,
            "batch_size": BATCH_SIZE, "overlength_policy": "reject",
            "reranker_instruction": RERANK_INSTRUCTION,
            "reranker_prefix": RERANK_PREFIX, "reranker_suffix": RERANK_SUFFIX,
            "reranker_score": "softmax([logit_no,logit_yes])[1] at last position",
            "reranker_logits_to_keep": 1,
        },
        "models": MODEL_FILES, "requested_device": args.device,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "machine": platform.machine(),
                        "packages": {name: version(name) for name in ("torch", "transformers", "qdrant-client")}},
        "protocol": "Prepare each mode, then one query per case in stored order; no warmup or tuning. Latencies are single-run observations.",
    }
    write_json(plan_path, plan)
    result = {"status": "RUNNING", "plan_file": plan_path.name, "plan_sha256": sha256(plan_path),
              "input_sha256": pins, "modes": [], "errors": []}
    write_json(args.output, result)
    try:
        with Retriever(rows, args.model_dir, args.device) as retriever:
            for mode in args.modes:
                mode_result = {"mode": mode, "status": "RUNNING", "queries": []}
                result["modes"].append(mode_result)
                try:
                    mode_result["preparation_delta_ms"] = retriever.prepare(mode)
                    for case in cases:
                        output = retriever.search(case["query"], mode=mode, limit=args.limit,
                                                  candidate_limit=args.candidate_limit)
                        ranked = [hit["id"] for hit in output["hits"]]
                        mode_result["queries"].append({
                            "id": case["id"], "relevant": case["relevant"],
                            "top1": bool(ranked and ranked[0] in case["relevant"]),
                            "topk": bool(set(ranked) & set(case["relevant"])), "result": output,
                        })
                        write_json(args.output, result)
                    mode_result["summary"] = {
                        "cases": len(cases), "top1_correct": sum(q["top1"] for q in mode_result["queries"]),
                        "topk_correct": sum(q["topk"] for q in mode_result["queries"]), "k": args.limit,
                    }
                    mode_result["status"] = "SUCCEEDED"
                except Exception as exc:
                    mode_result.update(status="FAILED", error={"type": type(exc).__name__, "message": str(exc)})
                    result["errors"].append({"mode": mode, **mode_result["error"]})
                write_json(args.output, result)
            result["preparation_ms"] = retriever.preparation_ms
            result["token_counts"] = retriever.token_counts
    except Exception as exc:
        result["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    if sha256(args.corpus) != pins["corpus"] or sha256(args.queries) != pins["queries"]:
        result["errors"].append({"type": "InputChanged", "message": "Corpus or queries changed during execution"})
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["status"] = "FAILED" if result["errors"] else "SUCCEEDED"
    write_json(args.output, result)
    print(json.dumps({"status": result["status"], "output": str(args.output),
                      "summaries": [{"mode": m["mode"], "status": m["status"],
                                     "summary": m.get("summary"), "error": m.get("error")} for m in result["modes"]]},
                     ensure_ascii=False))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
