"""Search English clauses or page-located reference chunks. Returns evidence only."""
import argparse
import json
from pathlib import Path

from retrieval import Retriever

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("query")
    p.add_argument("--corpus", type=Path, nargs="+", default=[ROOT/"data/corpus.json"])
    p.add_argument("--mode", choices=("bm25","hybrid","hybrid_rerank"), default="bm25")
    p.add_argument("--model-dir", type=Path, default=ROOT/".models")
    p.add_argument("--device", choices=("cpu","mps","cuda"), default="cpu")
    p.add_argument("--equipment")
    p.add_argument("--limit", type=int, default=3)
    args = p.parse_args()
    if not 1 <= args.limit <= 50:
        p.error("--limit must be between 1 and 50")
    rows = [row for path in args.corpus for row in json.loads(path.read_text())]
    indexed = {row["id"]:row for row in rows}
    with Retriever(rows,model_dir=args.model_dir,device=args.device) as search:
        result = search.search(args.query, mode=args.mode, limit=args.limit,
                               candidate_limit=max(20,args.limit),
                               filters={"equipment":args.equipment} if args.equipment else None)
    for hit in result["hits"]:
        row=indexed[hit["id"]]
        hit["passage"]={k:row[k] for k in ("text","ko","kind","source","revision")}
    result["decision"]="EVIDENCE_ONLY"
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__ == "__main__":
    main()
