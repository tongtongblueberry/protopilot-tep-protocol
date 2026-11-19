"""Start a pinned Qdrant service on loopback for the lifetime of one isolated demo."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.request import urlopen

RELEASE = "v1.16.2"
TARGETS = {
    ("Linux", "x86_64"): ("x86_64-unknown-linux-musl", "26cc11f4fbacfb5b1602d3eb1b6cf8d8edd64c9f152df69e5a82fdc1440341a0"),
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "d409dbfb018aeab4ca056c45456303bd8d87cc136aeabe9b612f2e80f29319ab"),
}


@contextmanager
def serve(cache, port=6333):
    key = platform.system(), platform.machine()
    target, expected = TARGETS[key]
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"qdrant-{target}.tar.gz"
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        uri=f"https://github.com/qdrant/qdrant/releases/download/{RELEASE}/{archive.name}"
        with tempfile.NamedTemporaryFile(dir=cache,delete=False) as scratch:
            temporary=Path(scratch.name)
        try:
            if shutil.which("curl"):
                subprocess.run(["curl","-fsSL","--retry","2","--max-time","180","-o",str(temporary),uri],check=True)
            else:
                with urlopen(uri,timeout=60) as stream, temporary.open("wb") as sink:
                    shutil.copyfileobj(stream,sink)
            if hashlib.sha256(temporary.read_bytes()).hexdigest()!=expected:
                raise ValueError("Qdrant archive digest mismatch")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="protopilot-qdrant-") as td:
        work=Path(td)
        binary=work/"qdrant"
        with tarfile.open(archive) as src:
            matches=[m for m in src if m.isfile() and Path(m.name).name=="qdrant"]
            if len(matches)!=1:
                raise ValueError("Expected one Qdrant executable")
            with src.extractfile(matches[0]) as handle, binary.open("wb") as sink:
                shutil.copyfileobj(handle,sink)
        binary.chmod(0o700)
        env={**os.environ, "QDRANT__SERVICE__HOST":"127.0.0.1","QDRANT__SERVICE__HTTP_PORT":str(port),
             "QDRANT__SERVICE__GRPC_PORT":str(port+1),"QDRANT__TELEMETRY_DISABLED":"true"}
        url=f"http://127.0.0.1:{port}"
        # Confirm ports are unused before startup; never attach to an existing service.
        import socket
        for test_port in (port,port+1):
            with socket.socket() as test:
                test.bind(("127.0.0.1",test_port))
        with (work/"qdrant.log").open("w+b") as log:
            proc=subprocess.Popen([str(binary)],cwd=work,env=env,stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    if proc.poll() is not None:
                        log.seek(0)
                        raise RuntimeError("Qdrant startup failed: "+log.read()[-1600:].decode(errors="replace"))
                    try:
                        with urlopen(url+"/readyz", timeout=1) as response:
                            if response.status==200:
                                break
                    except OSError:
                        pass
                    time.sleep(.2)
                else:
                    raise TimeoutError("Qdrant readiness timed out")
                with urlopen(url,timeout=2) as response:
                    identity=json.load(response)
                if identity.get("version") != RELEASE.lstrip("v"):
                    raise RuntimeError("Unexpected running Qdrant version")
                yield url, identity
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
