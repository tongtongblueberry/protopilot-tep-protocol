"""Run a ProtoPilot incident using a selected retrieval and decision backend."""
import argparse
import json
from pathlib import Path

from agent import ZoroHTTPBackend, fixture_runtime, load_registry, run_incident
from retrieval import Retriever

ROOT = Path(__file__).resolve().parent
CASES = ("compound", "recover", "missing", "wrong-mode", "normal", "stale", "wrong-unit", "other-equipment")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def demo_input(case):
    snapshot = read_json(ROOT / "data/sources/episode.json")
    batches = []
    if case in {"missing", "recover"}:
        hidden = [o for o in snapshot["observation_prefix"] if o["tag"] == "XMEAS(21)"]
        snapshot["observation_prefix"] = [o for o in snapshot["observation_prefix"] if o["tag"] != "XMEAS(21)"]
        if case == "recover":
            # A demo sensor lookup returns a real, already published sample from the fixture.
            batches = [{"observations": [hidden[-1]]}]
    elif case == "normal":
        snapshot["published_alarm_events"] = []
        snapshot["alarm_groups"] = []
    elif case == "wrong-mode":
        snapshot["mode"] = "UNREGISTERED_DEMO_MODE"
    elif case == "stale":
        snapshot["observation_prefix"] = [
            o for o in snapshot["observation_prefix"]
            if o["tag"] != "XMEAS(21)" or o["available_key"]["tick"] == 0
        ]
    elif case == "wrong-unit":
        for obs in snapshot["observation_prefix"]:
            if obs["tag"] == "XMEAS(7)":
                obs["unit"] = "psi"
    elif case == "other-equipment":
        for event in snapshot["published_alarm_events"]:
            event["affected_unit"] = "TEP_SEPARATOR"
            event["alarm_definition_id"] = "SEPARATOR-DEMO-UNKNOWN"
        snapshot["alarm_groups"] = []
    return snapshot, batches


def execute(rows, retriever, mode, *, case=None, event_path=None, runtime_path=None,
            backend=None, max_steps=4, origin=None):
    if event_path:
        supplied = read_json(event_path)
        snapshot = supplied.get("snapshot", supplied)
        runtime = read_json(runtime_path) if runtime_path else supplied.get("runtime", {})
        batches = supplied.get("observation_batches", [])
        declared = supplied.get("origin")
        if declared and origin and declared != origin:
            raise ValueError("event origin conflicts with --origin")
        origin = origin or declared
        if origin not in {"synthetic_fixture", "tep_replay"}:
            raise ValueError("custom events require an explicit origin: synthetic_fixture or tep_replay")
    else:
        snapshot, batches = demo_input(case)
        runtime = fixture_runtime()
        origin = "synthetic_fixture"
    packet = run_incident(snapshot, rows, retriever, runtime=runtime, registry=load_registry(),
                          origin=origin, retrieval_mode=mode, max_steps=max_steps,
                          decision_backend=backend, observation_batches=batches)
    packet["demo_case"] = case if not event_path else None
    packet["requested_retrieval_mode"] = mode
    return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--case", choices=CASES, default="compound")
    inputs.add_argument("--event", type=Path, help="JSON event or snapshot/runtime envelope")
    parser.add_argument("--origin", choices=("synthetic_fixture", "tep_replay"), help="Provenance for a custom event")
    parser.add_argument("--runtime", type=Path, help="Runtime contract for custom event")
    parser.add_argument("--mode", choices=("bm25", "hybrid", "hybrid_rerank"), default="bm25")
    parser.add_argument("--model-dir", type=Path, default=ROOT / ".models")
    parser.add_argument("--device", choices=("cpu", "mps", "cuda"), default="cpu")
    parser.add_argument("--zoro-endpoint", help="Authorized Trida /v1/systemone or /v1/decide URL")
    parser.add_argument("--max-steps", type=int, default=4)
    parser.add_argument("--output", type=Path, help="Save complete incident JSON")
    args = parser.parse_args()
    if not 1 <= args.max_steps <= 12:
        parser.error("--max-steps must be between 1 and 12")
    if (args.runtime or args.origin) and not args.event:
        parser.error("--runtime and --origin apply to --event")
    rows = read_json(ROOT / "data/corpus.json")
    backend = ZoroHTTPBackend(args.zoro_endpoint) if args.zoro_endpoint else None
    with Retriever(rows, model_dir=args.model_dir, device=args.device) as search:
        try:
            packet = execute(rows, search, args.mode, case=args.case, event_path=args.event,
                             runtime_path=args.runtime, backend=backend, max_steps=args.max_steps, origin=args.origin)
        except ValueError as exc:
            parser.error(str(exc))
    serialized = json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(exist_ok=True, parents=True)
        args.output.write_text(serialized+"\n", encoding="utf-8")
        print(packet["action"], args.output)
    else:
        print(serialized)


if __name__ == "__main__":
    main()
