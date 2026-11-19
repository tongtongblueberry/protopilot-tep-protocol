"""Exercise the frozen retrieval comparisons and operator incidents on one device."""
import argparse
import hashlib
import json
import os
import platform
import statistics
import time
from importlib.metadata import version
from pathlib import Path

from app import CASES, ROOT, execute, read_json
from retrieval import Retriever

MODES = ("bm25", "hybrid", "hybrid_rerank")


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def evaluate(search, rows, data, mode):
    scores = []
    for item in data:
        result = search.search(item["query"], mode=mode, limit=3, candidate_limit=len(rows))
        ids = [h["id"] for h in result["hits"]]
        relevant = set(item["relevant"])
        scores.append({"id": item["id"], "category":item.get("category","pilot"),"relevant":item["relevant"],
                       "ranked_ids":ids, "top1":bool(set(ids[:1]) & relevant) if relevant else None,
                       "top3":bool(set(ids) & relevant) if relevant else None,
                       "timing_ms":result["timing_ms"], "execution":result["execution"],
                       "hits":result["hits"]})
    positives = [s for s in scores if s["top1"] is not None]
    return {"n_positives":len(positives),"n_without_relevant_procedure":len(scores)-len(positives),
            "top1":sum(s["top1"] for s in positives),"top3":sum(s["top3"] for s in positives),
            "median_total_ms_all_queries":statistics.median(s["timing_ms"]["total"] for s in scores),
            "queries":scores}


def run(model_dir,device,destination, modes=MODES, qdrant_url=None):
    rows = read_json(ROOT/"data/corpus.json")
    destination.mkdir(parents=True, exist_ok=True)
    import torch
    if device == "cpu":
        torch.set_num_threads(min(4, os.cpu_count() or 1))
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA selected but unavailable")
    meta = {"device":device,"machine":platform.machine(),"python":platform.python_version(),
            "packages":{n:version(n) for n in ("torch","qdrant-client","transformers")},
            "gpu":torch.cuda.get_device_name(0) if device=="cuda" else None,
            "input_sha256":{f:hashlib.sha256((ROOT/"data"/f).read_bytes()).hexdigest() for f in ("corpus.json","pilot_queries.json","challenge.json")},
            "zoro":{"status":"NOT_RUN","actual_calls":0}}
    start = time.perf_counter()
    metrics = {"runtime":meta,"modes":{}}
    pilot = read_json(ROOT/"data/pilot_queries.json")
    challenge = read_json(ROOT/"data/challenge.json")
    with Retriever(rows,model_dir=model_dir,device=device,qdrant_url=qdrant_url) as search:
        for mode in modes:
            preparation = search.prepare(mode)
            metrics["modes"][mode] = {"preparation_delta_ms":preparation, "pilot":evaluate(search,rows,pilot,mode),
                                       "challenge":evaluate(search,rows,challenge,mode)}
            save(destination/"retrieval-results.json",metrics)
        cases = []
        for case in CASES:
            p = execute(rows, search, modes[-1], case=case)
            save(destination/(case+".json"),p)
            cases.append({"case":case,"action":p["action"],"evidence_ids":p["evidence_ids"],"backend":p["backend"],
                          "zoro_status":p["zoro_status"],"steps":len(p["trace"])})
        metrics["preparation_ms"]=search.preparation_ms
    metrics["elapsed_seconds"] = time.perf_counter()-start
    metrics["cases"]=cases
    save(destination/"retrieval-results.json",metrics)
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device",choices=["mps","cpu","cuda"],default="cpu")
    parser.add_argument("--model-dir",type=Path,default=ROOT/".models")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--qdrant-url",help="Loopback URL for a dedicated Qdrant service")
    parser.add_argument("--bm25-only",action="store_true")
    args = parser.parse_args()
    d=run(args.model_dir,args.device,args.output,("bm25",) if args.bm25_only else MODES, args.qdrant_url)
    print({m:(v["pilot"]["top1"],v["challenge"]["top1"]) for m,v in d["modes"].items()})
