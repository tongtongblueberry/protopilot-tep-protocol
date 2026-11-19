"""Validate the TEP demo bundle or walk a case using local published records."""

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import operator
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
EPS = 1e-9
ACTIONS = {
    "REQUEST_INFO": "추가 정보 요청",
    "PROVIDE_SOP": "운전 절차 안내",
    "RETRIEVE_AGAIN": "현행 조항 재검색",
    "ESCALATE": "운전 책임자 이관",
}
ROW_FIELDS = {"id", "text", "ko", "kind", "revision", "equipment", "mode", "tags", "source"}
SNAPSHOT_FIELDS = {
    "opaque_episode_id", "opportunity_id", "mode", "cutoff_key", "t_decision_cutoff_sim_h",
    "sensor_contract_rev", "observation_prefix", "published_alarm_events", "alarm_groups",
}
OBS_FIELDS = {
    "observation_id", "tag", "value_or_null", "unit", "quality", "sampled_at_sim_h_or_null",
    "available_at_sim_h", "available_key",
}
ALARM_FIELDS = {
    "alarm_event_id", "alarm_instance_id", "alarm_definition_id", "alarm_definition_revision",
    "affected_unit", "source_observation_ids", "detected_at_sim_h", "available_at_sim_h",
    "available_key", "condition_state", "ack_state", "observation_link_state",
}
GROUP_FIELDS = {"alarm_event_ids", "alarm_group_id", "alarm_instance_ids", "group_id",
                "member_event_ids", "public_reason"}
HIDDEN_LABEL = re.compile(r"\bIDV[\s_(-]*\d+\b|disturbance_id|expected_(decision|action|opening)", re.I)
VARIANTS = ("recorded", "wrong-revision", "unavailable", "stale-reading", "bad-reading",
            "invalid-approval", "close-without-signoff")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def canonical_sha(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path):
    def unique_object(fields):
        result = dict(fields)
        need(len(result) == len(fields), f"{path.name}: duplicate JSON object field")
        return result

    def reject_constant(value):
        raise ValueError(f"{path.name}: non-JSON numeric constant {value}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)


def index(rows, field):
    result = {row[field]: row for row in rows}
    need(len(result) == len(rows), f"중복 식별자 {field}")
    return result


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def event_key(key):
    need(set(key) == {"tick", "event_seq"}, "잘못된 공개 순서 필드")
    result = key["tick"], key["event_seq"]
    need(all(type(i) is int and i >= 0 for i in result), "잘못된 공개 순서 값")
    return result


def visible(row, cutoff, now):
    return (event_key(row["available_key"]) <= cutoff and finite(row["available_at_sim_h"])
            and 0 <= row["available_at_sim_h"] <= now + EPS)


def latest_observations(rows, cutoff, now):
    published = [row for row in rows if visible(row, cutoff, now)]
    return {row["tag"]: row for row in sorted(published, key=lambda row: event_key(row["available_key"]))}


def valid_observation(row, spec, now, tick_size):
    if row is None or not finite(row["value_or_null"]) or row["quality"] != spec["quality"]:
        return False
    sampled, available = row["sampled_at_sim_h_or_null"], row["available_at_sim_h"]
    delay = next((spec[key] for key in ("minimum_publication_delay_sim_h",
                                       "nominal_analysis_delay_sim_h", "analyzer_delay_sim_h")
                  if spec.get(key) is not None), 0)
    return (row["unit"] == spec["unit"] and finite(sampled) and 0 <= sampled <= available + EPS
            and available - sampled + EPS >= delay
            and now - sampled <= spec["max_age_ticks_inclusive"] * tick_size + EPS)


def validate_observation_rows(rows, cutoff, now):
    records = index(rows, "observation_id")
    tag_order = set()
    for row in rows:
        need(OBS_FIELDS <= row.keys() <= OBS_FIELDS | {"available_wall_ns"}, "공개 관측 필드 오류")
        need(visible(row, cutoff, now), f"공개 시점 이후의 관측 {row['observation_id']}")
        order = row["tag"], event_key(row["available_key"])
        need(order not in tag_order, f"한 시점에 같은 계기의 관측이 중복됨 {order}")
        tag_order.add(order)
        sampled, value = row["sampled_at_sim_h_or_null"], row["value_or_null"]
        need(sampled is None or (finite(sampled) and 0 <= sampled <= row["available_at_sim_h"] + EPS),
             f"관측 채취 시각 오류 {row['observation_id']}")
        need(value is None or finite(value), f"관측값은 유한수 또는 null이어야 함 {row['observation_id']}")
    return records


def validate_public_packet(packet):
    need(set(packet) == {"origin", "snapshot", "runtime"}, "공개 사건의 최상위 필드 오류")
    need(packet["origin"] in {"synthetic_fixture", "tep_replay"}, "공개 사건의 출처 오류")
    snapshot, runtime = packet["snapshot"], packet["runtime"]
    need(SNAPSHOT_FIELDS <= snapshot.keys() <= SNAPSHOT_FIELDS | {"action_catalog_public"}, "공개 스냅숏 필드 오류")
    need(not snapshot.get("action_catalog_public"), "조작 명령이 공개 입력에 들어 있음")
    need(set(runtime) == {"model_rev", "controller_rev", "episode_status", "tick_duration_sim_h"},
         "런타임 선언 필드 오류")
    need(not HIDDEN_LABEL.search(json.dumps(packet, ensure_ascii=False)), "평가 라벨이 공개 입력에 들어 있음")
    now = snapshot["t_decision_cutoff_sim_h"]
    cutoff = event_key(snapshot["cutoff_key"])
    dt = runtime["tick_duration_sim_h"]
    need(finite(now) and finite(dt) and dt > 0 and math.isclose(cutoff[0] * dt, now, abs_tol=EPS),
         "판단 시각과 공개 순서 불일치")
    obs = validate_observation_rows(snapshot["observation_prefix"], cutoff, now)
    alarms = snapshot["published_alarm_events"]
    index(alarms, "alarm_event_id")
    for row in alarms:
        need(ALARM_FIELDS <= row.keys() <= ALARM_FIELDS | {"available_wall_ns", "priority_id", "priority_reason"},
             "공개 경보 필드 오류")
        need(visible(row, cutoff, now) and finite(row["detected_at_sim_h"])
             and 0 <= row["detected_at_sim_h"] <= row["available_at_sim_h"] + EPS, "경보 시각 오류")
        need(row["condition_state"] in {"ACTIVE", "CLEARED"} and
             row["ack_state"] in {"ACKNOWLEDGED", "UNACKNOWLEDGED"}, "경보 상태 오류")
        sources = row["source_observation_ids"]
        need(row["observation_link_state"] == "COMPLETE" and sources and all(
            source in obs and visible(obs[source], event_key(row["available_key"]), row["available_at_sim_h"])
            for source in sources), f"경보 발생 근거를 당시 공개 관측에서 찾을 수 없음 {row['alarm_event_id']}")
    for group in snapshot["alarm_groups"]:
        need(group.keys() <= GROUP_FIELDS, "공개 경보 묶음에 등록되지 않은 필드가 있음")


def retrieval_observations(packet):
    """Provide public ground evidence without registry names that identify a case."""
    validate_public_packet(packet)
    snapshot = packet["snapshot"]
    return {
        "mode": snapshot["mode"],
        "t_decision_cutoff_sim_h": snapshot["t_decision_cutoff_sim_h"],
        "observations": [{key: row[key] for key in ("tag", "value_or_null", "quality", "unit",
                                                     "sampled_at_sim_h_or_null")}
                         for row in snapshot["observation_prefix"]],
        "alarm_conditions": [{key: row[key] for key in ("affected_unit", "alarm_definition_id",
                                                        "condition_state", "ack_state")}
                             for row in snapshot["published_alarm_events"]],
    }


def resolve(document, location):
    result = document
    for key in location.lstrip("/").split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        result = result[int(key)] if isinstance(result, list) else result[key]
    return result


def verify_structure(catalog, corpus, legacy, public, evaluation):
    revision = catalog["protocol_revision"]
    need(public["protocol_revision"] == evaluation["protocol_revision"] == revision, "파일 판본 불일치")
    need(evaluation["visibility"] == "EVALUATOR_ONLY", "평가 입력의 접근 구분이 없음")
    validate_public_packet(legacy)
    need(canonical_sha(legacy["snapshot"]) == catalog["legacy_baseline"]["public_snapshot_sha256"],
         "기존 RC-01 공개 입력 해시 불일치")

    sources = index(catalog["sources"], "source_id")
    cards = {}
    clauses = {}
    for entry in catalog["cards"]:
        payload, digest = entry["payload"], entry["payload_sha256"]
        need(digest == canonical_sha(payload) and digest not in cards, "카드 해시 오류 또는 중복")
        need(payload["execution_authority"] == "ADVISORY_ONLY", "카드에 설비 구동 권한이 있음")
        cards[digest] = payload
        for clause in payload["clauses"]:
            key = clause["clause_id"]
            need(key not in clauses and clause["source_refs"] and set(clause["source_refs"]) <= sources.keys(),
                 f"조항 ID 또는 출처 오류 {key}")
            clauses[key] = (digest, clause)
    rows = index(corpus, "id")
    need(rows.keys() == clauses.keys(), "검색 조항이 카드 조항과 일치하지 않음")
    for key, row in rows.items():
        digest, clause = clauses[key]
        payload, ref = cards[digest], row["source"]
        need(set(row) == ROW_FIELDS and row["kind"] == "sop" and re.search(r"[A-Za-z]{2}", row["text"])
             and row["ko"].strip() and not HIDDEN_LABEL.search(row["text"] + row["ko"]), f"검색 행 오류 {key}")
        need((row["text"], row["ko"], row["tags"]) == (clause["text"], clause["ko"], clause["tags"])
             and (row["revision"], row["equipment"], row["mode"])
             == (payload["revision"], payload["affected_unit"], payload["mode_scope"]), f"검색 행 불일치 {key}")
        need(set(ref) == {"uri", "locator", "payload_sha256"} and ref["uri"] == "tep_protocol.json"
             and ref["locator"].startswith("/cards/") and ref["payload_sha256"] == digest
             and resolve(catalog, ref["locator"]) == clause, f"검색 원문 위치 또는 해시 오류 {key}")

    profiles = index(catalog["tag_profiles"], "tag_profile_id")
    comparisons = index(catalog["comparisons"], "comparison_id")
    cases = index(catalog["cases"], "case_id")
    fixtures = index(evaluation["fixtures"], "case_id")
    public_packets = public["episodes"]
    need(set(cases) == set(fixtures) and len(cases) == len(public_packets), "사례, 공개 입력, 평가 입력 수 불일치")
    index([packet["snapshot"] for packet in public_packets], "opaque_episode_id")
    for number, (case, packet, fixture) in enumerate(zip(catalog["cases"], public_packets, evaluation["fixtures"])):
        case_id = case["case_id"]
        validate_public_packet(packet)
        need(case_id not in json.dumps(retrieval_observations(packet), ensure_ascii=False),
             f"검색용 관측에 사례 ID가 들어 있음 {case_id}")
        need(fixture["case_id"] == case_id
             and case["public_fixture_refs"] == [f"public_episodes.json#/episodes/{number}"]
             and fixture["public_fixture_ref"] == case["public_fixture_refs"][0]
             and case["evaluation_fixture_ref"] == f"eval_cases.json#/fixtures/{number}", f"사건 참조 오류 {case_id}")
        digest = case["active_card"]["payload_sha256"]
        card = cards[digest]
        need(all(case["active_card"][key] == card[key] for key in ("card_id", "revision")), f"사례 카드 불일치 {case_id}")
        profile = profiles[case["public_tag_profile_id"]]
        need(profile["sensor_contract_rev"] == card["sensor_contract_rev"] == packet["snapshot"]["sensor_contract_rev"],
             f"사례 계기 계약 불일치 {case_id}")
        need(not case["automatic_actions_allowed"] and not case["autonomous_closure_allowed"],
             f"사례에 자율 조작·종결 권한이 있음 {case_id}")
        need(comparisons[case["comparison_ref"]]["case_ref"] == case_id, f"비교 근거 참조 오류 {case_id}")
        rules = case["decision_rules"]
        need(len({r["priority"] for r in rules}) == len(rules), f"우선순위 중복 {case_id}")
        contract = catalog["decision_contracts"][case["decision_contract_id"]]
        for rule in rules:
            need(clauses[rule["clause_id"]][0] == digest and rule["then"]["action"] in ACTIONS
                 and rule["then"]["execution_authority"] == "ADVISORY_ONLY"
                 and all(c["fact"] in contract["facts"] for c in rule["when"]["all"]), f"판단 조항 불일치 {case_id}")
    return cards, rows, profiles, comparisons


def case_context(case, packet, profile, active_alarms):
    snapshot, runtime, applicable = packet["snapshot"], packet["runtime"], case["applicability"]
    if (snapshot["mode"] not in applicable["mode_scope"]
            or snapshot["sensor_contract_rev"] != profile["sensor_contract_rev"]
            or runtime["episode_status"] != applicable.get("episode_status_required", "RUNNING")
            or any(runtime[k] != applicable[k] for k in ("model_rev", "controller_rev"))):
        return False
    allowed = applicable["eligible_alarm_definitions"]
    if not allowed:
        return bool(applicable.get("invocation")) and not active_alarms
    return bool(active_alarms) and all(
        alarm["affected_unit"] == applicable["affected_unit"] and alarm["alarm_definition_id"] in allowed
        and alarm["alarm_definition_revision"] == applicable["alarm_definition_revision"]
        for alarm in active_alarms)


def active_alarms_at(snapshot, cutoff, now):
    alarms = [row for row in snapshot["published_alarm_events"] if visible(row, cutoff, now)]
    ordered = sorted(alarms, key=lambda row: event_key(row["available_key"]))
    return [row for row in {row["alarm_instance_id"]: row for row in ordered}.values()
            if row["condition_state"] == "ACTIVE"]


def compare_pattern(pattern, present, baseline, records):
    if not pattern or not pattern.get("checks"):
        return False
    for test in pattern["checks"]:
        kind, threshold = test["operator"], test.get("value")
        if "field" in test:
            if kind != "eq" or records.get(test["field"]) != threshold:
                return False
            continue
        if kind.startswith("ratio_delta_pp_"):
            first, second = test["tags"]
            values = [data.get(tag) for data in (present, baseline) for tag in (first, second)]
            if not all(finite(value) for value in values):
                return False
            a, c, earlier_a, earlier_c = values
            if min(values) < 0 or a + c <= 0 or earlier_a + earlier_c <= 0:
                return False
            value = abs(100 * (a / (a + c) - earlier_a / (earlier_a + earlier_c)))
            relation = operator.ge if kind.endswith("_ge") else operator.le
        else:
            tag = test["tag"]
            value = present.get(tag)
            if not finite(value):
                return False
            if kind == "lt_field":
                threshold, relation = present.get(test["against"]), operator.lt
            elif kind == "gt_baseline":
                threshold, relation = baseline.get(tag), operator.gt
            elif kind.startswith(("delta_pp_", "abs_delta_")):
                reference = baseline.get(tag)
                if not finite(reference):
                    return False
                value = abs(value - reference) if kind.startswith("abs_") else value - reference
                relation = operator.ge if kind.endswith("_ge") else operator.le
            else:
                relation = {"lt": operator.lt, "lte": operator.le, "gt": operator.gt,
                            "gte": operator.ge, "eq": operator.eq}[kind]
        if not finite(threshold) or not relation(value, threshold):
            return False
    return True


def approval_plan(case, records, comparison, now):
    evidence = case["required_evidence"]
    plans = [row for row in records if row.get("record_type") == evidence["approval_record_type"]
             and finite(row.get("published_at_sim_h")) and row["published_at_sim_h"] <= now + EPS]
    if not plans:
        return "missing", False
    plan = max(plans, key=lambda row: row["published_at_sim_h"])
    time_fields = ("published_at_sim_h", "effective_at_sim_h", "evaluation_start_sim_h",
                   "evaluation_end_sim_h", "expires_at_sim_h")
    time_values = [plan.get(k) for k in time_fields]
    valid = (set(evidence["approval_fields"]) <= set(plan) and plan["issuer_role"] == evidence["approval_role"]
             and plan["status"] == "approved" and bool(plan["record_id"])
             and all(finite(value) for value in time_values) and time_values == sorted(time_values)
             and time_values[0] <= now <= time_values[-1] + EPS)
    mapping = comparison["policy_review"]["simulator_mapping"]
    if "experimental_config" in mapping:
        config = mapping["experimental_config"]
        valid = (valid and plan["field"] == evidence["required_closed_loop_binding"] and plan["unit"] == mapping["unit"]
                 and plan["base_mode"] == mapping["mode"] and plan["intent"] == "simulator_setpoint_comparison"
                 and plan["prior_value"] == config["baseline"] and plan["approved_value"] == config["step_value"]
                 and plan["effective_at_sim_h"] == config["step_time"])
    else:
        valid = (valid and plan["unit"] == "mode_key" and plan["field"] == "scenario_mode_preset"
                 and plan["prior_value"] == plan["base_mode"] and plan["approved_value"] in mapping["scenario_modes"]
                 and plan["intent"] in {"independent_scenario", "continuous_transition"})
    return ("verified" if valid else "invalid"), plan.get("intent") == "continuous_transition"


def decide(case, packet, profile, catalog, comparison, corpus, candidate, records=(), *,
           tries=1, lookup_available=True, phase="prepare"):
    """Apply the published rule table without reading any expected decision."""
    validate_public_packet(packet)
    snapshot, runtime = packet["snapshot"], packet["runtime"]
    cutoff, now = event_key(snapshot["cutoff_key"]), snapshot["t_decision_cutoff_sim_h"]
    tick_size = runtime["tick_duration_sim_h"]
    latest = latest_observations(snapshot["observation_prefix"], cutoff, now)
    signals = index(profile["signals"], "tag")
    usable = {tag: row["value_or_null"] for tag, row in latest.items()
              if tag in signals and valid_observation(row, signals[tag], now, tick_size)}
    evidence = case["required_evidence"]
    initial, extra = evidence["initial_tags"], evidence["extra_tags"]
    missing_tags = [tag for tag in initial + extra if tag not in usable]
    alarms = active_alarms_at(snapshot, cutoff, now)
    context = case_context(case, packet, profile, alarms)
    ref = case["active_card"]
    matches = (candidate is not None and candidate == corpus.get(candidate["id"])
               and candidate["source"]["payload_sha256"] == ref["payload_sha256"]
               and snapshot["mode"] in candidate["mode"])
    fields = {}
    for field in profile.get("operator_records", []):
        values = [row for row in records if row.get("field") == field["key"] and visible(row, cutoff, now)
                  and row.get("confirmed_by") == field["source_role"] and row.get("value") in field["permitted_values"]]
        if values:
            fields[field["key"]] = max(values, key=lambda row: event_key(row["available_key"]))["value"]
    missing_fields = [field for field in evidence["operator_fields"] if field not in fields]
    bounds = catalog["decision_contracts"]["targets"]["constraints"]["normal_operating_guard"]["bounds"]
    if case["decision_contract_id"] == "feed":
        bounds = [bound for bound in bounds if bound["tag"] in {"XMEAS(7)", "XMEAS(9)"}]
    at_boundary = any(bound["tag"] in usable and (
        usable[bound["tag"]] >= bound.get("upper", math.inf)
        or usable[bound["tag"]] <= bound.get("lower", -math.inf)) for bound in bounds)
    stopped = runtime["episode_status"] in {"TRIPPED", "SHUTDOWN"}
    facts = {
        "snapshot_context_valid": context, "registered_case_context_valid": context,
        "operator_review_required": at_boundary or stopped, "operating_boundary_or_stop": at_boundary or stopped,
        "observed_operating_bound_reached": at_boundary or stopped, "retrieval_match": matches,
        "retrieval_matches_active_card": matches, "retrieval_remaining": tries > 0,
        "retrieval_retry_remaining": tries > 0, "retrieved_clause_current": matches,
        "required_observations_complete": not missing_tags, "invalid_or_missing_tags": missing_tags,
        "invalid_required_tags": missing_tags, "has_invalid_required_tags": bool(missing_tags),
        "lookup_remaining": lookup_available, "info_request_available": lookup_available,
        "outstanding_required_evidence_unavailable": not lookup_available,
    }
    family = case["decision_contract_id"]
    if family == "feed":
        baseline_key = event_key(evidence["prior_baseline_key"])
        baseline_time = baseline_key[0] * tick_size
        earlier = latest_observations(snapshot["observation_prefix"], baseline_key, baseline_time)
        baseline = {tag: row["value_or_null"] for tag, row in earlier.items()
                    if tag in signals and valid_observation(row, signals[tag], baseline_time, tick_size)}
        watch = evidence.get("specific_escalation_condition", {})
        facts.update(entry_data_valid=bool(baseline) and all(tag in usable for tag in initial),
                     requested_data_valid=not missing_fields and all(tag in usable for tag in extra),
                     specific_escalation_hit=bool(watch) and fields.get(watch["field"]) == watch["equals"],
                     pattern_evidence_matches=compare_pattern(evidence.get("case_pattern"), usable, baseline, fields),
                     approved_change_case_confirmed=compare_pattern(evidence.get("secondary_case_pattern"), usable,
                                                                    baseline, fields))
    elif family == "targets":
        plan_state, continuous = approval_plan(case, records, comparison, now)
        if plan_state == "verified":
            missing_fields = []
        facts.update(plan_state=plan_state, assessment_phase=phase, continuous_transition_requested=continuous,
                     postwindow_gh_pair_valid=False, closeout_evidence_complete=False)
        if not matches and tries == 0 and context and not (at_boundary or stopped):
            return {"action": "ESCALATE", "reason_ko": "현행 조항을 재검색했으나 적용 가능한 판본을 찾지 못했습니다."}

    for rule in sorted(case["decision_rules"], key=lambda item: item["priority"]):
        if all(condition["fact"] in facts and facts[condition["fact"]] == condition["equals"]
               for condition in rule["when"]["all"]):
            output = {**rule["then"], "rule_id": rule["rule_id"], "clause_id": rule["clause_id"]}
            tags = facts[output["request_tags_from_fact"]] if "request_tags_from_fact" in output else output.get("request_tags", [])
            records_requested = output.get("request_fields", [])
            if output.get("request_only_missing_or_invalid"):
                tags = [tag for tag in tags if tag in missing_tags]
                records_requested = [record for record in records_requested if record in missing_fields]
            if output["action"] == "REQUEST_INFO":
                output.update(request_tags=tags, request_fields=records_requested)
            output["cutoff_sim_h"] = now
            return output
    return {"action": "ESCALATE", "reason_ko": "등록 절차에서 이 정보의 조합을 확인하지 못했습니다."}


def publish_lookup(packet, lookup):
    """Move the public clock only when the recorded lookup is delivered."""
    result = deepcopy(packet)
    snapshot = result["snapshot"]
    old_key, old_time = event_key(snapshot["cutoff_key"]), snapshot["t_decision_cutoff_sim_h"]
    new_key, new_time = event_key(lookup["new_cutoff_key"]), lookup["t_decision_cutoff_sim_h"]
    need(new_key > old_key and new_time > old_time, "자료 수신에서 공개 시점이 앞으로 진행되지 않음")
    rows = lookup["observation_rows"]
    need(all(event_key(row["available_key"]) > old_key and row["available_at_sim_h"] > old_time for row in rows),
         "이미 공개된 관측이 추가 조회에 포함됨")
    for record in lookup["operator_records"]:
        available = record.get("available_at_sim_h", record.get("published_at_sim_h"))
        need(finite(available) and old_time < available <= new_time + EPS, "운전자 기록을 공개할 수 없는 시각")
        if "available_key" in record:
            need(old_key < event_key(record["available_key"]) <= new_key, "운전자 기록 공개 순서 오류")
    snapshot["cutoff_key"] = lookup["new_cutoff_key"]
    snapshot["t_decision_cutoff_sim_h"] = new_time
    snapshot["observation_prefix"].extend(rows)
    validate_public_packet(result)
    return result


def revision_review(comparison):
    review = comparison["policy_review"]
    if comparison["physical_effect_evidence"] != "OBSERVED_MATCHED_PAIR":
        return {"decision_ko": "개정 보류", "reason_ko": "같은 출발 상태에서 현행 대응과 개정안을 수행한 결과가 필요합니다.",
                "comparison_id": comparison["comparison_id"]}
    archive = comparison["archive_evidence"]
    keep, lower = archive["keep"], archive["fixed_lower"]
    pressure = lower["reactor_peak_pressure_kPa_g"] - keep["reactor_peak_pressure_kPa_g"]
    volume = (lower["product_mixture_volume_m3"] - keep["product_mixture_volume_m3"]) * 1000
    reported = archive["delta_lower_minus_keep"]
    need(math.isclose(pressure, reported["reactor_peak_pressure_kPa"], abs_tol=1e-6)
         and math.isclose(volume, reported["product_mixture_volume_L"], abs_tol=1e-6), "저장 비교 결과와 계산이 다름")
    need(review["held_out"]["outcome"] == "UNKNOWN", "이 빌드에서 대조할 독립 모의 기록의 범위가 바뀌었습니다.")
    return {"decision_ko": "현행 유지 및 개정 보류", "comparison_id": comparison["comparison_id"],
            "reason_ko": "별도의 냉각수 조건에서 같은 출발점으로 비교한 기록이 필요합니다.",
            "archive_window_h": archive["window_h"], "peak_pressure_delta_kPa": round(pressure, 5),
            "product_volume_delta_L": round(volume, 2), "shutdown": [keep["shutdown"], lower["shutdown"]],
            "sources": archive["source_refs"]}


def select_row(case, packet, rows):
    """Choose corroborating evidence for the already selected advisory case."""
    ref = case["active_card"]
    tags = {reading["tag"] for reading in retrieval_observations(packet)["observations"]}
    eligible = [row for row in rows.values() if row["source"]["payload_sha256"] == ref["payload_sha256"]]
    return max(eligible, key=lambda row: len(set(row["tags"]) & tags), default=None)


def recorded_closure(case, packet, profile, closure, records, catalog):
    """Return the recorded feed decision only when the closure evidence qualifies."""
    rule = next(entry["close_checks"] for entry in case["exit"] if "close_checks" in entry)
    finished = publish_lookup(packet, {"new_cutoff_key": closure["cutoff_key"],
                                      "t_decision_cutoff_sim_h": closure["t_decision_cutoff_sim_h"],
                                      "observation_rows": closure["observations"], "operator_records": records})
    snapshot, runtime = finished["snapshot"], finished["runtime"]
    cutoff, now = event_key(snapshot["cutoff_key"]), snapshot["t_decision_cutoff_sim_h"]
    signals = index(profile["signals"], "tag")
    recent = latest_observations(closure["observations"], cutoff, now)
    readings = {tag: row["value_or_null"] for tag, row in recent.items()
                if valid_observation(row, signals[tag], now, runtime["tick_duration_sim_h"])}
    for field, role, value in (("A_FEED_SUPPLY_STATUS", "requires_source_role", "requires_source_status"),
                               ("SHIFT_DISPOSITION", "requires_disposition_role", "requires_disposition")):
        if not any(record["field"] == field and record["confirmed_by"] == rule[role]
                   and record["value"] == rule[value] for record in records):
            return "OPEN"
    limits = catalog["decision_contracts"]["feed"]["facts"]["observed_operating_bound_reached"]
    context_ok = case_context(case, finished, profile, active_alarms_at(snapshot, cutoff, now))
    if (context_ok and set(rule["required_valid_follow_up_tags"]) <= readings.keys()
            and compare_pattern({"checks": rule["authoring_fixture_checks"]}, readings, {}, {})
            and readings[limits["pressure_tag"]] < limits["pressure_gte_kpa_gauge"]
            and readings[limits["temperature_tag"]] < limits["temperature_gte_celsius"]):
        return "CLOSE"
    return "OPEN"


def walk(case, packet, fixture, bundle, *, variant="recorded"):
    catalog, rows, profiles, comparisons = bundle
    profile = profiles[case["public_tag_profile_id"]]
    compare = comparisons[case["comparison_ref"]]
    row = select_row(case, packet, rows)
    if variant == "wrong-revision":
        row = {**row, "revision": "example-obsolete-revision"}
    elif variant in {"stale-reading", "bad-reading"}:
        packet = deepcopy(packet)
        tag = case["required_evidence"]["initial_tags"][0]
        for reading in packet["snapshot"]["observation_prefix"]:
            if reading["tag"] == tag:
                if variant == "stale-reading":
                    reading["sampled_at_sim_h_or_null"] = 0
                else:
                    reading["quality"] = "BAD"
    arguments = case, packet, profile, catalog, compare, rows, row
    opening = decide(*arguments)
    result = {"case_id": case["case_id"], "title_ko": case["title_ko"], "opening": opening}
    if variant in {"wrong-revision", "unavailable", "stale-reading", "bad-reading"}:
        options = {"tries": 0} if variant == "wrong-revision" else {"lookup_available": False}
        result["after_lookup"] = decide(*arguments, **options)
        return result
    if opening["action"] != "REQUEST_INFO":
        return result
    lookup = fixture["lookup_batch"]
    published = publish_lookup(packet, lookup)
    attachments = deepcopy(lookup["operator_records"])
    record_kinds = {value.get("record_type", value.get("field")) for value in attachments}
    published_tags = {value["tag"] for value in lookup["observation_rows"]}
    need(record_kinds == set(opening.get("request_fields", []))
         and set(opening.get("request_tags", [])) <= published_tags, "조회 결과가 요청한 자료와 일치하지 않습니다.")
    if variant == "invalid-approval":
        need(case["decision_contract_id"] == "targets", "승인 판정 예제는 SP 사례를 선택하세요.")
        attachments[0]["issuer_role"] = "unregistered_role"
    phase = "close" if variant == "close-without-signoff" else "prepare"
    after = decide(case, published, profile, catalog, compare, rows, row, attachments, phase=phase)
    if after["action"] == "REQUEST_INFO" and phase == "prepare":
        after = decide(case, published, profile, catalog, compare, rows, row, attachments, lookup_available=False)
    result.update(published_lookup={"cutoff_sim_h": published["snapshot"]["t_decision_cutoff_sim_h"],
                                    "observation_tags": sorted(published_tags),
                                    "operator_records": sorted(record_kinds)}, after_lookup=after,
                  revision_review=revision_review(compare))
    closure = fixture.get("closure_example")
    if closure and "observations" in closure and after["action"] == "PROVIDE_SOP":
        result["closure"] = recorded_closure(case, published, profile, closure, closure["operator_records"], catalog)
    return result


def check_expected(actual, expected, case_id):
    for key in ("action", "clause_id", "rule_id"):
        if expected.get(key):
            need(actual.get(key) == expected[key], f"{case_id}: {key} {actual.get(key)} != {expected[key]}")
    for field in ("request_tags", "request_fields"):
        need(set(actual.get(field, [])) == set(expected.get(field, [])), f"{case_id}: {field} does not match")


def verify_scenarios(catalog, public, evaluation, bundle):
    results = []
    entries = list(zip(catalog["cases"], public["episodes"], evaluation["fixtures"]))
    for case, packet, fixture in entries:
        result = walk(case, packet, fixture, bundle)
        check_expected(result["opening"], fixture["expected_opening"], case["case_id"])
        check_expected(result["after_lookup"], fixture["lookup_batch"]["expected_after_publication"], case["case_id"])
        if "closure" in result:
            need(result["closure"] == fixture["closure_example"]["expected_lifecycle"], "종료 기록과 절차 불일치")
        results.append(result)
    variants = {}
    choices = {entry[0]["case_id"]: entry for entry in entries}
    for variant, case_id, expected in (
        ("wrong-revision", "HX-CASE-R01", ("RETRIEVE_AGAIN", "ESCALATE")),
        ("unavailable", "HX-CASE-R01", ("REQUEST_INFO", "ESCALATE")),
        ("stale-reading", "HX-CASE-R01", ("REQUEST_INFO", "ESCALATE")),
        ("bad-reading", "HX-CASE-R01", ("REQUEST_INFO", "ESCALATE")),
        ("invalid-approval", "SP-RATE", ("REQUEST_INFO", "ESCALATE")),
        ("close-without-signoff", "SP-RATE", ("REQUEST_INFO", "REQUEST_INFO")),
    ):
        event = walk(*choices[case_id], bundle, variant=variant)
        got = event["opening"]["action"], event["after_lookup"]["action"]
        need(got == expected, f"{variant}: {got} != {expected}")
        if variant == "close-without-signoff":
            need("supervisor_closeout" in event["after_lookup"]["request_fields"], "종료 확인 기록이 요청되지 않음")
        if variant in {"stale-reading", "bad-reading"}:
            need("XMEAS(7)" in event["opening"]["request_tags"], "부적합 계기를 다시 요청하지 않음")
        variants[variant] = {"case_id": case_id, "actions": got}
    case, packet, fixture = choices["FD-A-FLOW"]
    closure = fixture["closure_example"]
    records = [row for row in closure["operator_records"] if row["field"] != "SHIFT_DISPOSITION"]
    status = recorded_closure(case, publish_lookup(packet, fixture["lookup_batch"]),
                              bundle[2][case["public_tag_profile_id"]], closure, records, catalog)
    need(status == "OPEN", "교대 책임자의 확인이 빠진 사건이 닫힘")
    variants["feed-closure-without-signoff"] = {"case_id": case["case_id"], "status": status}
    return results, variants


def show(result, clauses):
    print(f"{result['case_id']}  {result['title_ko']}")
    for stage, label in (("opening", "첫 판단"), ("after_lookup", "후속 판단")):
        if stage not in result:
            continue
        outcome = result[stage]
        print(f"{label}  {ACTIONS[outcome['action']]} ({outcome['action']})")
        if outcome.get("clause_id"):
            row = clauses[outcome["clause_id"]]
            print(f"  {row['id']} · {row['revision']} · {row['source']['locator']}")
            print(f"  {outcome.get('action_ko', row['ko'])}")
        elif outcome.get("reason_ko"):
            print(f"  {outcome['reason_ko']}")
        request = outcome.get("request_tags", []) + outcome.get("request_fields", [])
        if request:
            print("  요청 항목  " + ", ".join(request))
    if "closure" in result:
        state = "책임자 종료 기록 확인" if result["closure"] == "CLOSE" else "종료 확인 대기"
        print(f"사건 상태  {state}")
    if "revision_review" in result:
        review = result["revision_review"]
        print(f"매뉴얼 검토  {review['decision_ko']}")
        if "peak_pressure_delta_kPa" in review:
            print(f"  보관 비교에서 감량 시 최고 압력 {review['peak_pressure_delta_kPa']:+.2f} kPa, "
                  f"제품 혼합물 {review['product_volume_delta_L']:+.2f} L")
        print(f"  {review['reason_ko']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--demo", action="store_true", help="반응기 냉각 사례를 재생")
    selection.add_argument("--case", help="프로토콜의 사례 ID를 지정해 재생")
    parser.add_argument("--variant", choices=VARIANTS, default="recorded")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 표시")
    args = parser.parse_args()
    if args.variant != "recorded" and not (args.demo or args.case):
        parser.error("--variant에는 --demo 또는 --case가 필요합니다.")
    try:
        catalog, corpus, legacy, public, evaluation = (
            read_json(ROOT / name) for name in ("tep_protocol.json", "rag_corpus.json", "public_episode.json",
                                                "public_episodes.json", "eval_cases.json"))
        _, rows, profiles, comparisons = verify_structure(catalog, corpus, legacy, public, evaluation)
        bundle = catalog, rows, profiles, comparisons
        if args.demo or args.case:
            case_id = args.case or "HX-CASE-R01"
            if args.variant == "close-without-signoff" and not case_id.startswith("SP-"):
                parser.error("종료 확인 기록 예제는 SP 사례를 선택하세요.")
            cases = {case["case_id"]: (case, packet, fixture) for case, packet, fixture in
                     zip(catalog["cases"], public["episodes"], evaluation["fixtures"])}
            need(case_id in cases, f"사례 없음 {case_id}")
            result = walk(*cases[case_id], bundle, variant=args.variant)
            if not args.json:
                show(result, rows)
                return
        else:
            outputs, variants = verify_scenarios(catalog, public, evaluation, bundle)
            result = {"status": "PASS", "protocol_revision": catalog["protocol_revision"],
                      "catalog_canonical_sha256": canonical_sha(catalog), "corpus_canonical_sha256": canonical_sha(corpus),
                      "clauses": len(rows), "public_episodes": len(outputs),
                      "opening_actions": dict(Counter(x["opening"]["action"] for x in outputs)),
                      "after_lookup_actions": dict(Counter(x["after_lookup"]["action"] for x in outputs)),
                      "variants": variants}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"검사 실패  {exc}\n")


if __name__ == "__main__":
    main()
