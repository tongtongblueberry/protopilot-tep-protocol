"""Fetch the two public retrieval checkpoints. Only verified model files are reused."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

MODELS = {
    "bge": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181",
            ("config.json", "tokenizer_config.json", "tokenizer.json", "special_tokens_map.json", "sentencepiece.bpe.model", "pytorch_model.bin")),
    "reranker": ("Qwen/Qwen3-Reranker-0.6B", "e61197ed45024b0ed8a2d74b80b4d909f1255473",
                 ("config.json", "tokenizer.json", "tokenizer_config.json", "merges.txt", "vocab.json", "model.safetensors", "generation_config.json")),
}


def matches(path, record):
    if not path.is_file() or path.stat().st_size != record["size"]:
        return False
    lfs = record.get("lfs")
    h = hashlib.sha256() if lfs else hashlib.sha1()
    if not lfs:
        h.update(f"blob {record['size']}\0".encode())
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest() == (lfs["sha256"] if lfs else record["blobId"])


def fetch(cache, name):
    repo, revision, files = MODELS[name]
    dest = cache / name
    dest.mkdir(parents=True, exist_ok=True)
    with urlopen(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true", timeout=30) as r:
        meta = json.load(r)
    if meta["sha"] != revision:
        raise ValueError("Unexpected revision for " + repo)
    records = {r["rfilename"]: r for r in meta["siblings"]}
    for filename in files:
        target = dest / filename
        record = records[filename]
        if matches(target, record):
            continue
        fd, tmpname = tempfile.mkstemp(dir=dest, prefix=filename+".", suffix=".part")
        os.close(fd)
        tmp = Path(tmpname)
        try:
            url = f"https://huggingface.co/{repo}/resolve/{revision}/{filename}"
            if shutil.which("curl"):
                subprocess.run(["curl", "-fsSL", "--retry", "3", "--connect-timeout", "20", "--max-time", "1800",
                                "--output", str(tmp), url], check=True)
            else:
                with urlopen(url, timeout=90) as source, tmp.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            if not matches(tmp, record):
                raise ValueError("File digest mismatch: " + filename)
            tmp.replace(target)
            print(name, filename, record["size"], flush=True)
        finally:
            tmp.unlink(missing_ok=True)
    return name, revision


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=2) as pool:
        print(dict(pool.map(lambda name: fetch(args.directory, name), MODELS)))
