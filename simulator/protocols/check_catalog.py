"""Verify a TEP catalog, precomputed corpus, and every referenced public episode.

This checker uses the current RAG registration without registering new cards.
Its public-observation queries are integration probes, not a held-out benchmark.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit


LEGACY_TAGS = {"XMEAS(7)", "XMEAS(8)", "XMEAS(9)", "XMEAS(21)"}
EVALUATOR_KEYS = {"expected_first", "expected_opening", "expected_action", "ground_truth", "hidden_fault_id",
                  "evaluator_only", "benchmark_inputs", "lookup_batch", "counterchecks"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pointer(document, locator):
    locator = locator.removeprefix("#")
    if not locator:
        return document
    if not locator.startswith("/"):
        raise ValueError("Expected JSON Pointer")
    value = document
    for segment in locator[1:].split("/"):
        key = segment.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def public_reference(reference, catalog_path, package_root):
    uri = urlsplit(reference)
    relative = Path(unquote(uri.path))
    if uri.scheme or uri.netloc or uri.query or relative.is_absolute():
        raise ValueError("Public fixture references must be local relative files")
    path = (catalog_path.parent / relative).resolve()
    path.relative_to(package_root)
    return path, pointer(read(path), uri.fragment)


def evaluator_fields(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in EVALUATOR_KEYS or key.lower().startswith("idv_"):
                found.append(path + "/" + key)
            found.extend(evaluator_fields(item, path + "/" + key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(evaluator_fields(item, path + "/" + str(index)))
    return found


def observed_query(snapshot, agent):
    """Project public measurements and alarms, without a case, card, or label input."""
    cutoff = agent.event_key(snapshot["cutoff_key"])
    when = snapshot["t_decision_cutoff_sim_h"]
    readings, reading_errors = agent.public_records(snapshot["observation_prefix"], cutoff, when, agent.OBS_FIELDS)
    alarms, alarm_errors = agent.public_records(snapshot["published_alarm_events"], cutoff, when, agent.ALARM_FIELDS)
    if reading_errors or alarm_errors:
        raise ValueError("Invalid public record projection")
    latest = {reading["tag"]: reading for reading in readings}
    alarm_latest = {event["alarm_instance_id"]: event for event in alarms}
    active = [event for event in alarm_latest.values() if event["condition_state"] == "ACTIVE"]
    observed = [f"{tag} = {reading['value_or_null']} {reading['unit']} ({reading['quality']})"
                for tag, reading in sorted(latest.items())]
    public_alarms = [f"{event['alarm_definition_id']} on {event['affected_unit']}" for event in active]
    query = (f"Process mode {snapshot['mode']}. Public observations at simulation hour {when}: "
             + "; ".join(observed) + ". Active alarms: " + (", ".join(public_alarms) or "none published")
             + ". Which current procedure covers these observations and what evidence or authorization should be checked?")
    filters = {"process_mode": snapshot["mode"]}
    equipment = {event["affected_unit"] for event in active}
    if len(equipment) == 1:
        filters["equipment"] = next(iter(equipment))
    return query, filters, sorted(latest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rag-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    projections = parser.add_mutually_exclusive_group(required=True)
    projections.add_argument("--corpus", type=Path, help="Standalone precomputed corpus JSON")
    projections.add_argument("--adapter-input", type=Path, help="Builder bundle containing rag_corpus and public_episodes")
    parser.add_argument("--expected-kind", choices=("research_sop", "sop"), default="research_sop")
    parser.add_argument("--package-root", type=Path, help="Boundary for fixture references; defaults to catalog directory")
    parser.add_argument("--catalog-uri", help="Exact source.uri expected in every corpus row; defaults to catalog filename")
    parser.add_argument("--input-status", choices=("provisional", "frozen"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.rag_root.resolve()
    catalog_path = args.catalog.resolve()
    package_root = (args.package_root or catalog_path.parent).resolve()
    catalog_uri = args.catalog_uri or catalog_path.name
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    import agent
    import app
    from retrieval import Retriever

    checks, files = [], {}

    def track(path, label):
        files[label] = {"path": path, "sha256": digest(path)}

    def check(name, passed, detail=None):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    track(catalog_path, "catalog")
    for name in ("agent.py", "app.py", "retrieval.py", "data/corpus.json", "data/sources/rc01.json"):
        track(root / name, "rag/" + name)
    catalog = read(catalog_path)
    public_projection = None
    if args.adapter_input:
        adapter_path = args.adapter_input.resolve()
        track(adapter_path, "adapter_input")
        adapter = read(adapter_path)
        check("adapter_catalog_hash", adapter["catalog_canonical_sha256"] == agent.canonical_sha(catalog))
        check("adapter_protocol_revision", adapter["protocol_revision"] == catalog["protocol_revision"])
        rows = adapter["rag_corpus"]
        public_projection = adapter["public_episodes"]
        # Evaluation data can share the file, but is excluded from both inputs.
        del adapter
        check("public_projection_revision", public_projection["protocol_revision"] == catalog["protocol_revision"])
    else:
        corpus_path = args.corpus.resolve()
        track(corpus_path, "corpus")
        rows = read(corpus_path)
    registry = agent.load_registry()
    check("incoming_clause_ids_disjoint_from_legacy", not set(row["id"] for row in rows).intersection(row["id"] for row in registry["rows"]))
    for field, key in (("cases", "case_id"), ("families", "family_id"), ("tag_profiles", "tag_profile_id"), ("sources", "source_id")):
        values = [entry[key] for entry in catalog[field]]
        check(field + "_ids_unique", len(values) == len(set(values)), {"count": len(values)})
    cards, clauses = {}, {}
    source_ids = {source["source_id"] for source in catalog["sources"]}
    for index, card in enumerate(catalog["cards"]):
        payload = card["payload"]
        identity = payload["card_id"], payload["revision"]
        check("card_identity_unique/" + str(index), identity not in cards, identity)
        check("card_payload_hash/" + str(index), agent.canonical_sha(payload) == card["payload_sha256"])
        cards[identity] = card
        for clause_index, clause in enumerate(payload["clauses"]):
            cid = clause["clause_id"]
            check("clause_identity_unique/" + cid, cid not in clauses)
            clauses[cid] = (payload, clause, card["payload_sha256"], f"/cards/{index}/payload/clauses/{clause_index}")
            check("clause_sources_exist/" + cid, set(clause.get("source_refs", [])).issubset(source_ids))
    row_ids = [row["id"] for row in rows]
    check("corpus_covers_all_clauses_once", len(row_ids) == len(set(row_ids)) and set(row_ids) == set(clauses),
          {"rows": len(rows), "clauses": len(clauses), "missing": sorted(set(clauses) - set(row_ids)), "extra": sorted(set(row_ids) - set(clauses))})
    for row in rows:
        cid = row["id"]
        if cid not in clauses:
            continue
        payload, clause, sha, locator = clauses[cid]
        problems = []
        expected = {"text": clause["text"], "ko": clause["ko"], "tags": clause["tags"], "equipment": payload["affected_unit"],
                    "mode": payload["mode_scope"], "revision": payload["revision"], "kind": args.expected_kind}
        problems.extend(key for key, value in expected.items() if row.get(key) != value)
        source = row.get("source", {})
        if source.get("uri") != catalog_uri:
            problems.append("source.uri")
        if source.get("payload_sha256") != sha:
            problems.append("source.payload_sha256")
        try:
            if pointer(catalog, source.get("locator", "")) != clause:
                problems.append("source.locator")
        except (KeyError, IndexError, TypeError, ValueError):
            problems.append("source.locator")
        check("corpus_source_and_content/" + cid, not problems, problems)

    profiles = {profile["tag_profile_id"]: profile for profile in catalog["tag_profiles"]}
    families = {family["family_id"] for family in catalog["families"]}
    search_results, incident_results, indexed_rows, fixture_refs = [], [], [], []
    with Retriever(rows) as search:
        # Exact-text probes check that every validated row was actually indexed.
        for row in rows:
            result = search.search(row["text"], mode="bm25", limit=len(rows), candidate_limit=max(20, len(rows)))
            ids = [hit["id"] for hit in result["hits"]]
            indexed_rows.append({"id": row["id"], "retrieved": row["id"] in ids})
        check("all_rows_reachable_in_index", all(item["retrieved"] for item in indexed_rows), {"count": len(indexed_rows)})
        for case in catalog["cases"]:
            case_id = case["case_id"]
            active = case["active_card"]
            identity = active["card_id"], active["revision"]
            check("case_card_and_profile/" + case_id, identity in cards and case["public_tag_profile_id"] in profiles and case["family_id"] in families)
            if identity not in cards or case["public_tag_profile_id"] not in profiles:
                continue
            card = cards[identity]
            payload = card["payload"]
            profile = profiles[case["public_tag_profile_id"]]
            check("case_card_hash/" + case_id, active["payload_sha256"] == card["payload_sha256"])
            check("case_sensor_contract/" + case_id, payload["sensor_contract_rev"] == profile["sensor_contract_rev"])
            allowed_tags = {signal["tag"] for signal in profile["signals"]}
            expected_ids = {clause["clause_id"] for clause in payload["clauses"]}
            check("case_has_public_fixture/" + case_id, bool(case["public_fixture_refs"]))
            for number, reference in enumerate(case["public_fixture_refs"]):
                identifier = case_id + "--" + str(number)
                if public_projection is not None:
                    uri = urlsplit(reference)
                    if uri.scheme or uri.netloc or uri.query or uri.path != catalog["data_contract"]["actor_projection"]:
                        raise ValueError("Bundled public reference differs from the catalog actor projection")
                    envelope = pointer(public_projection, uri.fragment)
                else:
                    path, envelope = public_reference(reference, catalog_path, package_root)
                    track(path, "public/" + str(path.relative_to(package_root)))
                fixture_refs.append(reference)
                snapshot = envelope["snapshot"]
                runtime = envelope["runtime"]
                check("public_envelope_has_no_evaluator_fields/" + identifier, not evaluator_fields(envelope), evaluator_fields(envelope))
                check("public_declared_origin/" + identifier, envelope.get("origin") in {"synthetic_fixture", "tep_replay"})
                context_matches = (snapshot["mode"] in payload["mode_scope"] and snapshot["sensor_contract_rev"] == profile["sensor_contract_rev"]
                                   and all(runtime.get(key) == payload[key] for key in ("model_rev", "controller_rev")))
                check("public_context_matches_authored_case/" + identifier, context_matches)
                tags = {reading["tag"] for reading in snapshot["observation_prefix"]}
                check("public_tags_declared/" + identifier, tags.issubset(allowed_tags), sorted(tags - allowed_tags))
                query, filters, visible_tags = observed_query(snapshot, agent)
                result = search.search(query, mode="bm25", limit=5, candidate_limit=max(20, len(rows)), filters=filters)
                hits = [hit["id"] for hit in result["hits"]]
                check("case_search_executed/" + identifier, result["execution"]["executed_mode"] == "bm25" and bool(hits))
                check("case_search_returns_known_evidence/" + identifier, set(hits).issubset(clauses))
                search_results.append({"case_id": case_id, "public_fixture_ref": reference, "query": query, "filters": filters,
                                       "visible_tags": visible_tags, "extended_sensor_profile": bool(allowed_tags - LEGACY_TAGS),
                                       "expected_active_card_clauses": sorted(expected_ids), "active_card_in_top5": bool(set(hits) & expected_ids),
                                       "result": result})
                event_path = out / "public-events" / (identifier + ".json")
                save(event_path, envelope)
                packet = app.execute(rows, search, "bm25", event_path=event_path)
                save(out / "incidents" / (identifier + ".json"), packet)
                check("unregistered_case_holds_guidance/" + identifier,
                      packet["action"] == "ESCALATE" and not packet["guidance_ko"] and not packet["evidence_ids"] and not packet["citations"])
                check("all_incoming_rows_rejected/" + identifier, set(packet.get("rejected_row_ids", [])) == set(row_ids))
                check("case_uses_rules_no_zoro/" + identifier, packet["backend"] == "deterministic_rules" and packet["zoro_status"] == "NOT_RUN")
                poison = deepcopy(envelope)
                marker = "EVALUATOR_ONLY_MARKER_71A8"
                poison["expected_action"] = marker
                poison["snapshot"]["hidden_fault_id"] = marker
                poison["runtime"]["hidden_label"] = marker
                future = deepcopy(snapshot["observation_prefix"][-1])
                future.update(observation_id=marker, available_key={"tick": snapshot["cutoff_key"]["tick"] + 100, "event_seq": 999},
                              available_at_sim_h=snapshot["t_decision_cutoff_sim_h"] + 100.0, value_or_null=987654321.0)
                poison["snapshot"]["observation_prefix"].append(future)
                protected_query = observed_query(poison["snapshot"], agent)
                check("query_hidden_future_invariance/" + identifier, (query, filters, visible_tags) == protected_query and marker not in query)
                protected = agent.run_incident(poison["snapshot"], rows, search, runtime=poison["runtime"], origin=poison["origin"])
                base_states = [step["decision_input"] for step in packet["trace"] if "decision_input" in step]
                protected_states = [step["decision_input"] for step in protected["trace"] if "decision_input" in step]
                check("decision_hidden_future_invariance/" + identifier, base_states == protected_states and marker not in json.dumps(protected, ensure_ascii=False))
                incident_results.append({"case_id": case_id, "public_fixture_ref": reference, "action": packet["action"], "gate": packet["gate"],
                                         "guidance_count": len(packet["guidance_ko"]), "zoro_status": packet["zoro_status"]})
    check("all_cases_executed", {case["case_id"] for case in catalog["cases"]} == {item["case_id"] for item in search_results})
    if public_projection is not None:
        declared_refs = {catalog["data_contract"]["actor_projection"] + "#/episodes/" + str(index)
                         for index in range(len(public_projection["episodes"]))}
        check("all_bundled_public_episodes_referenced", set(fixture_refs) == declared_refs)
    check("all_inputs_unchanged", all(digest(record["path"]) == record["sha256"] for record in files.values()))
    summary = {"input_status": args.input_status, "checker_sha256": digest(Path(__file__).resolve()), "query_policy": "public_observation_v1", "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
               "at_utc": datetime.now(timezone.utc).isoformat(), "checks": checks, "total": len(checks), "passed": sum(item["passed"] for item in checks),
               "case_count": len(catalog["cases"]), "corpus_rows": len(rows), "public_episode_count": len(search_results),
               "projection_layout": "bundled" if public_projection is not None else "standalone", "expected_kind": args.expected_kind,
               "corpus_canonical_sha256": agent.canonical_sha(rows),
               "input_sha256": {label: record["sha256"] for label, record in files.items()},
               "index_probe": {"row_count": len(indexed_rows), "retrieved_count": sum(item["retrieved"] for item in indexed_rows), "meaning": "exact-text reachability only"},
               "public_query_coverage": {"n": len(search_results), "active_card_in_top5": sum(item["active_card_in_top5"] for item in search_results),
                                         "extended_sensor_cases": sum(item["extended_sensor_profile"] for item in search_results)},
               "incidents": incident_results, "actual_zoro_calls": 0,
               "limits": ["Case labels are used only for post-search coverage scoring.", "No retrieval relevance acceptance threshold was imposed.",
                          "The current legacy registry intentionally rejects all new cards.", "This does not execute the expanded protocol's new decision rules or validate industrial operating guidance."]}
    save(out / "checks.json", summary)
    save(out / "case-searches.json", search_results)
    save(out / "row-index-probes.json", indexed_rows)
    print(json.dumps({key: summary[key] for key in ("input_status", "status", "total", "passed", "case_count", "corpus_rows", "public_query_coverage")}))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
