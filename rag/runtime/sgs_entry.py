"""Bootstrap the public retrieval models inside a temporary PyTorch SGS Pod."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from qdrant_server import serve

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "remote_output"
OUT.mkdir(exist_ok=True)
progress = {"state":"STARTED", "steps":{}}


def step(name, command):
    started = time.monotonic()
    subprocess.run(command, cwd=ROOT, check=True, timeout=1200)
    progress["steps"][name]=round(time.monotonic()-started,3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    args = parser.parse_args()
    env = {"TOKENIZERS_PARALLELISM":"false", "HF_HUB_DISABLE_TELEMETRY":"1", "PIP_DISABLE_PIP_VERSION_CHECK":"1"}
    # The cluster runs this image under an unprivileged numeric UID.
    home = ROOT / ".ephemeral-home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), PYTHONUSERBASE=str(home / "packages"), HF_HOME=str(home / "hf"))
    os.environ.update(env)
    try:
        step("dependencies",[sys.executable,"-m","pip","install","--user","--no-cache-dir","--disable-pip-version-check",
                             "-r","runtime/requirements.txt"])
        step("public_models",[sys.executable,"runtime/fetch_models.py",".models"])
        with serve(ROOT / ".server-cache") as (url, identity):
            progress["qdrant"] = identity
            step("suite",[sys.executable,"run_suite.py","--device",args.device,"--model-dir",".models",
                          "--qdrant-url",url,"--output",str(OUT)])
        progress["state"]="SUCCEEDED"
    except Exception as exc:
        progress["state"]="FAILED"
        progress["error"]=str(exc)
        traceback.print_exc()
    finally:
        files = {f.stem:json.loads(f.read_text()) for f in sorted(OUT.glob("*.json")) if f.name != "bundle.json"}
        (OUT/"bundle.json").write_text(json.dumps({"run":progress,"outputs":files},ensure_ascii=False,separators=(",",":"))+"\n")
    return 0 if progress["state"]=="SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
