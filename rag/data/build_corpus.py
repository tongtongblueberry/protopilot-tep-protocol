"""Build bilingual demo passages from the research card and linked reference notes."""
import hashlib
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
CARD = DATA / "sources/rc01.json"
ORIGIN = "data/sources/rc01.json"
EXPECTED_CARD = "3a6522e333eb8790f28729da3db5018be29a5ca68c2a9ae463edebf500ac1e53"
ENGLISH_STEPS = [
    "Review each published alarm against its raw event history, affected equipment, onset, clearance and acknowledgement state. An acknowledgement records receipt and does not clear an active condition.",
    "Check the operating mode, procedure source and revision. Verify the time, quality and units of all required visible sensor readings before applying the procedure.",
    "Record the possible causes and which clues are missing. Reactor temperature, pressure, level and cooling water outlet temperature do not distinguish a change in coolant inlet conditions from a flow or actuation problem. Inlet temperature and actual flow are not available in this base observation set.",
    "Record MONITOR under normal conditions. With sufficient evidence and no authorized intervention, retain the baseline or monitor. When a compatible document or necessary observations are missing, record abstention, the reason and the next check.",
    "Maintain baseline control and follow subsequent published observations until the actual episode termination can be verified.",
]
REFERENCES = [
    ("TEP-SIGNALS","TEP_REACTOR","Continuous measurements XMEAS(7), (8), (9), and (21) correspond to reactor pressure, level, temperature, and reactor cooling water outlet temperature.","반응기 압력·수위·온도 및 냉각수 출구 온도의 변수 정의.","https://users.abo.fi/khaggblo/RS/Downs.pdf#page=5","Table 4, continuous process measurements"),
    ("TEP-SEPARATOR","TEP_SEPARATOR","Product separator temperature, level and pressure are measured in the vapor–liquid separation unit.","분리기 온도·수위·압력의 계기 정의.","https://users.abo.fi/khaggblo/RS/Downs.pdf#page=5","Table 4, XMEAS(11–13)"),
    ("APM-STICTION","TEP_REACTOR","In the teaching simulation a sticking reactor cooling valve produces a cycling temperature trace. This simulated disturbance is an experiment, not an operator procedure.","냉각수 밸브 고착은 APMonitor 모사 실험의 한 사례다.","https://apmonitor.com/apps/tennessee-eastman/instructions.php","A sticking valve (Case 7)"),
    ("APM-LOSTFEED","TEP_FEED","Loss of A feed in Case 8 changes material supply to the process. The teaching exercise monitors the response of the stripper inventory.","원료 A 공급 중단 교육 시나리오는 스트리퍼 재고 변화를 추적한다.","https://apmonitor.com/apps/tennessee-eastman/instructions.php","A lost feed and the override (Case 8)"),
]

def canonical(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def build():
    card = json.loads(CARD.read_text())
    if canonical(card) != EXPECTED_CARD:
        raise ValueError("RC-01 source differs from the registered fixture")
    rows = []
    for step, en in zip(card["steps"], ENGLISH_STEPS, strict=True):
        n = step["step"]
        ko = step["instruction"]
        locator = f"steps[{n-1}]"
        if n == 3:
            ko += " " + card["diagnostic_rule"]["unresolved_reason"]
            locator += " and diagnostic_rule"
        rows.append(dict(id=f"RC01-S{n}", text=en, ko=ko, kind="research_sop", revision=card["revision"],
                         equipment=card["affected_unit"], mode=card["mode_scope"], tags=[o["tag"] for o in card["required_observations"]],
                         source=dict(uri=ORIGIN, locator=locator, payload_sha256=EXPECTED_CARD)))
    for id_, unit, en, ko, uri, locator in REFERENCES:
        rows.append(dict(id=id_, equipment=unit, kind="reference", revision="demo-reference-1", mode=[], tags=[],
                         text=en, ko=ko, source=dict(uri=uri, locator=locator, payload_sha256=None)))
    return rows

if __name__ == "__main__":
    items = build()
    (DATA/"corpus.json").write_text(json.dumps(items, ensure_ascii=False, indent=2)+"\n")
    print(len(items), canonical(items))
