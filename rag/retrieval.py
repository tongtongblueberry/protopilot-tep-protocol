"""Qdrant BM25/BGE-M3 retrieval and Qwen3 yes/no reranking.

Each instance owns a collection in memory or on an explicit loopback server. Model files are read only from the
explicit local model directory, checked against pinned upstream file digests.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from qdrant_client import QdrantClient, models


MODEL_FILES = {
  "bge": {
    "repo": "BAAI/bge-m3",
    "revision": "5617a9f61b028005a4858fdac845db406aefb181",
    "files": {
      "config.json": {
        "size": 687,
        "digest": "e6eda1c72da8f9dc30fdd9b69c73d35af3b7a7ad",
        "algorithm": "git_blob_sha1"
      },
      "pytorch_model.bin": {
        "size": 2271145830,
        "digest": "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38",
        "algorithm": "sha256"
      },
      "sentencepiece.bpe.model": {
        "size": 5069051,
        "digest": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
        "algorithm": "sha256"
      },
      "special_tokens_map.json": {
        "size": 964,
        "digest": "b1879d702821e753ffe4245048eee415d54a9385",
        "algorithm": "git_blob_sha1"
      },
      "tokenizer.json": {
        "size": 17098108,
        "digest": "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08",
        "algorithm": "sha256"
      },
      "tokenizer_config.json": {
        "size": 444,
        "digest": "dc69ac559dcba2694012009aaa108c614541789a",
        "algorithm": "git_blob_sha1"
      }
    }
  },
  "reranker": {
    "repo": "Qwen/Qwen3-Reranker-0.6B",
    "revision": "e61197ed45024b0ed8a2d74b80b4d909f1255473",
    "files": {
      "config.json": {
        "size": 727,
        "digest": "39784456e9de7a06590f8a4a38760ac1fb38d65e",
        "algorithm": "git_blob_sha1"
      },
      "generation_config.json": {
        "size": 214,
        "digest": "e4f1d3193e99a3e5d7047edf56e93a6e933fa31b",
        "algorithm": "git_blob_sha1"
      },
      "merges.txt": {
        "size": 1671853,
        "digest": "31349551d90c7606f325fe0f11bbb8bd5fa0d7c7",
        "algorithm": "git_blob_sha1"
      },
      "model.safetensors": {
        "size": 1191588280,
        "digest": "27cd75a405b9c1b46b59abfd88aaa209e6fed2a1972cde9b70e7659537c5e65b",
        "algorithm": "sha256"
      },
      "tokenizer.json": {
        "size": 11422654,
        "digest": "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
        "algorithm": "sha256"
      },
      "tokenizer_config.json": {
        "size": 9706,
        "digest": "7345216a0785dc7086e8c245b2a9d3896ce2b756",
        "algorithm": "git_blob_sha1"
      },
      "vocab.json": {
        "size": 2776833,
        "digest": "4783fe10ac3adce15ac8f358ef5462739852c569",
        "algorithm": "git_blob_sha1"
      }
    }
  }
}
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 2
BGE_MAX_TOKENS = 512
RERANK_MAX_TOKENS = 1024
BATCH_SIZE = 4
RERANK_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
RERANK_PREFIX = (
    '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query '
    'and the Instruct provided. Note that the answer can only be "yes" or "no".'
    '<|im_end|>\n<|im_start|>user\n'
)
RERANK_SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
MODES = ("bm25", "hybrid", "hybrid_rerank")


def canonical_hash(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def tokenize(text):
    """Keep unigrams and numbers, including process tag components."""
    return re.findall(r"\b\w+\b", text.casefold())


class RetrievalError(RuntimeError):
    """The requested retrieval backend could not be executed."""


def _verify_model(directory, name):
    for filename, expected in MODEL_FILES[name]["files"].items():
        path = directory / filename
        if not path.is_file() or path.stat().st_size != expected["size"]:
            raise RetrievalError(f"Model file is missing or incomplete: {name}/{filename}")
        digest = hashlib.sha256() if expected["algorithm"] == "sha256" else hashlib.sha1()
        if expected["algorithm"] == "git_blob_sha1":
            digest.update(f"blob {expected['size']}\0".encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected["digest"]:
            raise RetrievalError(f"Model file digest mismatch: {name}/{filename}")


def _validate_rows(rows):
    copied = deepcopy(list(rows))
    seen = set()
    for row in copied:
        for field in ("id", "text", "equipment", "kind", "revision", "ko"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"Each corpus row needs a nonempty {field}")
        if row["id"] in seen:
            raise ValueError(f"Duplicate corpus ID: {row['id']}")
        seen.add(row["id"])
        for field in ("mode", "tags"):
            if not isinstance(row.get(field), list) or any(not isinstance(x, str) for x in row[field]):
                raise ValueError(f"Each corpus row needs a string list for {field}")
        if not isinstance(row.get("source"), dict):
            raise ValueError("Each corpus row needs a source object")
        for field in ("uri", "locator"):
            if not isinstance(row["source"].get(field), str) or not row["source"][field]:
                raise ValueError(f"Each source needs a nonempty {field}")
        if not tokenize(row["text"]):
            raise ValueError(f"Corpus text has no searchable tokens: {row['id']}")
    return copied


class Retriever:
    """Immutable corpus with lazy neural models and explicit retrieval modes.

    ``filters`` accepts exact equipment, kind, revision and process_mode values.
    A document with an empty mode list does not match a process_mode filter.
    Retrieval relevance never establishes that a procedure is safe to apply.
    """

    def __init__(self, rows, model_dir=None, device="cpu", qdrant_url=None):
        started = time.perf_counter()
        self.rows = _validate_rows(rows)
        self.corpus_sha256 = canonical_hash(self.rows)
        self.model_dir = Path(model_dir) if model_dir else Path(__file__).resolve().parent / ".models"
        self.device = str(device)
        self._lock = threading.RLock()
        self._closed = False
        self._encoder = None
        self._reranker = None
        self._dense_ready = False
        self._torch = None
        self.preparation_ms = {"bm25_index": 0.0, "bge_load": 0.0, "dense_index": 0.0, "reranker_load": 0.0}
        self.token_counts = {"documents": [], "queries": [], "rerank_pairs": []}
        self._counts = [Counter(tokenize(row["text"])) for row in self.rows]
        self._vocabulary = sorted({term for counts in self._counts for term in counts})
        self._term_ids = {term: i for i, term in enumerate(self._vocabulary)}
        doc_freq = Counter(term for counts in self._counts for term in counts)
        self._idf = {term: math.log1p((len(self.rows) - freq + 0.5) / (freq + 0.5)) for term, freq in doc_freq.items()}
        self._average_length = sum(sum(counts.values()) for counts in self._counts) / max(len(self.rows), 1)
        self._server = qdrant_url is not None
        if self._server:
            endpoint = urlsplit(qdrant_url)
            if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.username is not None
                    or endpoint.password is not None or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
                raise ValueError("The demo Qdrant service must be on HTTP loopback")
        self._client = QdrantClient(url=qdrant_url) if self._server else QdrantClient(":memory:")
        self._collection = "sop_" + uuid4().hex if self._server else "sop"
        self._client.create_collection(
            self._collection,
            vectors_config={"dense": models.VectorParams(size=1024, distance=models.Distance.COSINE)},
            sparse_vectors_config={"bm25": models.SparseVectorParams()},
        )
        points = [models.PointStruct(id=i, vector={"bm25": self._document_vector(counts)}, payload={
            "id": row["id"], "equipment": row["equipment"], "kind": row["kind"],
            "revision": row["revision"], "process_mode": row["mode"],
        }) for i, (row, counts) in enumerate(zip(self.rows, self._counts))]
        if points:
            self._client.upsert(self._collection, points=points, wait=True)
        self.preparation_ms["bm25_index"] = (time.perf_counter() - started) * 1000

    def _document_vector(self, counts):
        length_ratio = sum(counts.values()) / self._average_length
        terms = sorted(counts, key=self._term_ids.__getitem__)
        values = [self._idf[term] * counts[term] * (BM25_K1 + 1) /
                  (counts[term] + BM25_K1 * (1 - BM25_B + BM25_B * length_ratio)) for term in terms]
        return models.SparseVector(indices=[self._term_ids[term] for term in terms], values=values)

    def _query_vector(self, query):
        counts = Counter(tokenize(query))
        terms = sorted(set(counts) & self._term_ids.keys(), key=self._term_ids.__getitem__)
        return models.SparseVector(indices=[self._term_ids[term] for term in terms],
                                   values=[float(counts[term]) for term in terms])

    def _configure_device(self):
        import torch

        target = torch.device(self.device)
        if target.type not in {"cpu", "mps", "cuda"}:
            raise RetrievalError(f"Unsupported device: {self.device}")
        if target.type == "mps" and not torch.backends.mps.is_available():
            raise RetrievalError("MPS is unavailable")
        if target.type == "cuda" and not torch.cuda.is_available():
            raise RetrievalError("CUDA is unavailable")
        self._torch = torch

    def _synchronize(self):
        if self._torch is None:
            return
        if self.device.startswith("cuda"):
            self._torch.cuda.synchronize(self.device)
        elif self.device.startswith("mps"):
            self._torch.mps.synchronize()

    def _load_encoder(self):
        from transformers import AutoModel, AutoTokenizer

        started = time.perf_counter()
        directory = self.model_dir / "bge"
        _verify_model(directory, "bge")
        self._configure_device()
        tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        encoder, info = AutoModel.from_pretrained(
            directory, local_files_only=True, trust_remote_code=False, add_pooling_layer=False,
            torch_dtype=self._torch.float32, output_loading_info=True,
        )
        if info["missing_keys"] or info["mismatched_keys"]:
            raise RetrievalError("BGE-M3 checkpoint has missing or mismatched model parameters")
        encoder = encoder.to(self.device).eval()
        self._encoder_tokenizer, self._encoder = tokenizer, encoder
        self._synchronize()
        self.preparation_ms["bge_load"] = (time.perf_counter() - started) * 1000

    def _encode(self, texts, *, document=False):
        vectors = []
        lengths = []
        for start in range(0, len(texts), BATCH_SIZE):
            encoded = self._encoder_tokenizer(texts[start:start + BATCH_SIZE], padding=True, truncation=False,
                                              return_tensors="pt")
            batch_lengths = encoded["attention_mask"].sum(dim=1).tolist()
            if max(batch_lengths) > BGE_MAX_TOKENS:
                raise RetrievalError(f"BGE-M3 input exceeds {BGE_MAX_TOKENS} tokens; rechunk the document or query")
            lengths.extend(batch_lengths)
            encoded = encoded.to(self.device)
            with self._torch.inference_mode():
                output = self._encoder(**encoded).last_hidden_state[:, 0]
                output = self._torch.nn.functional.normalize(output, p=2, dim=1)
            if not self._torch.isfinite(output).all().item():
                raise RetrievalError("BGE-M3 produced nonfinite vectors")
            vectors.extend(output.cpu().tolist())
        self.token_counts["documents" if document else "queries"].extend(lengths)
        return vectors

    def _load_reranker(self):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        started = time.perf_counter()
        directory = self.model_dir / "reranker"
        _verify_model(directory, "reranker")
        self._configure_device()
        tokenizer = AutoTokenizer.from_pretrained(directory, padding_side="left", local_files_only=True,
                                                  trust_remote_code=False)
        reranker, info = AutoModelForCausalLM.from_pretrained(
            directory, local_files_only=True, trust_remote_code=False, torch_dtype=self._torch.float32,
            output_loading_info=True,
        )
        if info["missing_keys"] or info["mismatched_keys"]:
            raise RetrievalError("Qwen checkpoint has missing or mismatched model parameters")
        reranker = reranker.to(self.device).eval()
        self._rerank_tokenizer, self._reranker = tokenizer, reranker
        self._label_ids = [tokenizer.convert_tokens_to_ids(label) for label in ("no", "yes")]
        if any(tokenizer.encode(label, add_special_tokens=False) != [token_id]
               for label, token_id in zip(("no", "yes"), self._label_ids)):
            raise RetrievalError("Reranker labels must each be one tokenizer token")
        self._prefix_ids = tokenizer.encode(RERANK_PREFIX, add_special_tokens=False)
        self._suffix_ids = tokenizer.encode(RERANK_SUFFIX, add_special_tokens=False)
        self._synchronize()
        self.preparation_ms["reranker_load"] = (time.perf_counter() - started) * 1000

    def prepare(self, mode="hybrid_rerank"):
        """Load and index before timing queries. Returns preparation work done here."""
        if mode not in MODES:
            raise ValueError(f"Unknown retrieval mode: {mode}")
        with self._lock:
            self._ensure_open()
            before = dict(self.preparation_ms)
            if mode != "bm25" and self.rows:
                if self._encoder is None:
                    self._load_encoder()
                if not self._dense_ready:
                    started = time.perf_counter()
                    dense = self._encode([row["text"] for row in self.rows], document=True)
                    self._client.update_vectors(self._collection, points=[
                        models.PointVectors(id=i, vector={"dense": vector}) for i, vector in enumerate(dense)
                    ], wait=True)
                    self._dense_ready = True
                    self.preparation_ms["dense_index"] = (time.perf_counter() - started) * 1000
            if mode == "hybrid_rerank" and self.rows and self._reranker is None:
                self._load_reranker()
            return {key: self.preparation_ms[key] - value for key, value in before.items()}

    def _rerank(self, query, points):
        scores = []
        for start in range(0, len(points), BATCH_SIZE):
            prompts = [f"<Instruct>: {RERANK_INSTRUCTION}\n<Query>: {query}\n<Document>: {self.rows[p.id]['text']}"
                       for p in points[start:start + BATCH_SIZE]]
            inputs = self._rerank_tokenizer(prompts, padding=False, truncation=False, return_attention_mask=False)
            ids = [self._prefix_ids + tokens + self._suffix_ids for tokens in inputs["input_ids"]]
            lengths = [len(tokens) for tokens in ids]
            if max(lengths) > RERANK_MAX_TOKENS:
                raise RetrievalError(f"Qwen input exceeds {RERANK_MAX_TOKENS} tokens; rechunk the document or query")
            self.token_counts["rerank_pairs"].extend(lengths)
            batch = self._rerank_tokenizer.pad({"input_ids": ids}, padding=True, return_tensors="pt").to(self.device)
            with self._torch.inference_mode():
                # Only the final-position logits are used by the official scorer.
                logits = self._reranker(**batch, use_cache=False, logits_to_keep=1).logits[:, -1, self._label_ids]
                yes_scores = self._torch.softmax(logits.float(), dim=1)[:, 1]
            if not self._torch.isfinite(yes_scores).all().item():
                raise RetrievalError("Qwen reranker produced nonfinite scores")
            scores.extend(yes_scores.cpu().tolist())
        return scores

    @staticmethod
    def _filter(filters):
        if filters is None:
            return None
        if not isinstance(filters, dict):
            raise ValueError("filters must be an object")
        supported = {"equipment", "kind", "process_mode", "revision"}
        if set(filters) - supported:
            raise ValueError("Unsupported filter keys: " + ", ".join(sorted(set(filters) - supported)))
        conditions = []
        for key, value in filters.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"Filter {key} must be a nonempty exact string")
            conditions.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
        return models.Filter(must=conditions) if conditions else None

    def _execution(self, mode, candidate_limit, filters):
        used = [] if mode == "bm25" else ["bge"]
        if mode == "hybrid_rerank":
            used.append("reranker")
        return {
            "requested_mode": mode, "executed_mode": mode, "backend": "qdrant_server" if self._server else "qdrant_local_memory",
            "device": self.device if used else "cpu", "dtype": "float32" if used else None,
            "models": {key: {"repo": MODEL_FILES[key]["repo"], "revision": MODEL_FILES[key]["revision"],
                             "files_verified": True} for key in used},
            "corpus_sha256": self.corpus_sha256, "candidate_limit": candidate_limit, "filters": deepcopy(filters),
            "preparation_ms": dict(self.preparation_ms),
        }

    def search(self, query, *, mode, limit=3, candidate_limit=20, filters=None):
        if not isinstance(query, str):
            raise ValueError("query must be text")
        if mode not in MODES:
            raise ValueError(f"Unknown retrieval mode: {mode}")
        if type(limit) is not int or type(candidate_limit) is not int or limit < 1 or candidate_limit < limit:
            raise ValueError("Positive integer limits must satisfy candidate_limit >= limit")
        qfilter = self._filter(filters)
        with self._lock:
            self._ensure_open()
            started = time.perf_counter()
            timing = {"prepare": 0.0, "query_embedding": 0.0, "retrieval": 0.0, "rerank": 0.0, "total": 0.0}
            result = {"hits": [], "timing_ms": timing, "execution": self._execution("bm25", candidate_limit, filters)}
            if not self.rows or not tokenize(query) or self._client.count(self._collection, count_filter=qfilter).count == 0:
                result["execution"].update(requested_mode=mode, executed_mode="empty", device="cpu")
                timing["total"] = (time.perf_counter() - started) * 1000
                return result
            setup_start = time.perf_counter()
            self.prepare(mode)
            timing["prepare"] = (time.perf_counter() - setup_start) * 1000
            sparse = self._query_vector(query)
            dense = None
            if mode != "bm25":
                embed_start = time.perf_counter()
                dense = self._encode([query])[0]
                timing["query_embedding"] = (time.perf_counter() - embed_start) * 1000
            query_start = time.perf_counter()
            sparse_points = self._client.query_points(self._collection, query=sparse, using="bm25",
                                                       query_filter=qfilter, limit=candidate_limit).points
            dense_points = []
            points = sparse_points
            if dense is not None:
                dense_points = self._client.query_points(self._collection, query=dense, using="dense",
                                                          query_filter=qfilter, limit=candidate_limit).points
                points = self._client.query_points(
                    self._collection,
                    prefetch=[models.Prefetch(query=sparse, using="bm25", filter=qfilter, limit=candidate_limit),
                              models.Prefetch(query=dense, using="dense", filter=qfilter, limit=candidate_limit)],
                    query=models.RrfQuery(rrf=models.Rrf(k=RRF_K)), limit=candidate_limit,
                ).points
            timing["retrieval"] = (time.perf_counter() - query_start) * 1000
            bm25_ranks = {p.id: rank for rank, p in enumerate(sparse_points, 1)}
            dense_ranks = {p.id: rank for rank, p in enumerate(dense_points, 1)}
            rerank_scores = None
            if mode == "hybrid_rerank" and points:
                rank_start = time.perf_counter()
                rerank_scores = self._rerank(query, points)
                timing["rerank"] = (time.perf_counter() - rank_start) * 1000
            hits = []
            for i, point in enumerate(points):
                score = point.score if rerank_scores is None else rerank_scores[i]
                hits.append({"id": self.rows[point.id]["id"], "score": float(score),
                             "bm25_rank": bm25_ranks.get(point.id), "dense_rank": dense_ranks.get(point.id),
                             "rrf_score": float(point.score) if dense is not None else None,
                             "rerank_score": rerank_scores[i] if rerank_scores is not None else None})
            hits.sort(key=lambda h: (-h["score"], h["id"]))
            for rank, hit in enumerate(hits, 1):
                hit["rank"] = rank
            result["hits"] = hits[:limit]
            result["execution"] = self._execution(mode, candidate_limit, filters)
            result["trace"] = {
                "bm25": [{"id": self.rows[p.id]["id"], "score": float(p.score)} for p in sparse_points],
                "dense": [{"id": self.rows[p.id]["id"], "score": float(p.score)} for p in dense_points],
                "candidates": hits,
            }
            timing["total"] = (time.perf_counter() - started) * 1000
            return result

    def _ensure_open(self):
        if self._closed:
            raise RetrievalError("Retriever is closed")

    def close(self):
        with self._lock:
            if not self._closed:
                try:
                    if self._server:
                        self._client.delete_collection(self._collection)
                finally:
                    self._client.close()
                self._encoder = None
                self._reranker = None
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
