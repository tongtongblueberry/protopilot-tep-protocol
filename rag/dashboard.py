"""Render one or more saved, evidence-bearing incidents into an offline Korean dashboard."""
import argparse
import html
import json
from pathlib import Path

LABEL = {"PROVIDE_SOP":"절차 안내","REQUEST_INFO":"추가 관측 요청","RETRIEVE_AGAIN":"절차 재검색","ESCALATE":"운영자 검토"}
TITLES = {"compound":"반응기 온도·압력 복합 알람", "recover":"냉각수 관측 추가 조회", "missing":"냉각수 측정값 누락",
          "wrong-mode":"적용 범위 밖의 운전 모드", "normal":"활성 알람 없음", "stale":"냉각수 관측 시효 초과",
          "wrong-unit":"압력 단위 불일치","other-equipment":"미등록 분리기 알람"}


def esc(value):
    return html.escape(str(value), quote=True)


def json_text(data):
    return esc(json.dumps(data, indent=2, ensure_ascii=False))


def block(packet, lookup, index):
    action = packet.get("action", "")
    code = packet.get("demo_case")
    title = TITLES.get(code, code or "입력 사건")
    guidance = packet.get("guidance_ko") or []
    if isinstance(guidance,str):
        guidance = [guidance]
    instruction = "".join("<li>"+esc(s if isinstance(s,str) else s.get("instruction_ko",s.get("ko",s)))+"</li>" for s in guidance)
    evidence = []
    for cid in packet.get("evidence_ids", []):
        row = lookup.get(cid)
        if row:
            src = row["source"]
            evidence.append(f'<article><div class="meta">{esc(cid)} · {esc(row["revision"])} · {esc(src["locator"])}</div><p>{esc(row["ko"])}</p><p lang="en">{esc(row["text"])}</p></article>')
    trace = packet.get("trace", [])
    contexts = [t["decision_input"]["context"] for t in trace if "decision_input" in t]
    context = contexts[-1] if contexts else {}
    readings = []
    for tag, observation in context.get("observations",{}).items():
        val = observation.get("value_or_null")
        readings.append(f'<span><b>{esc(tag)}</b> {esc(val if val is not None else "미확인")} {esc(observation.get("unit",""))}</span>')
    alarms = ", ".join(f"{a.get('alarm_definition_id','')} ({a.get('condition_state','')})" for a in context.get("active_alarms",[]))
    path = " → ".join(LABEL.get(t.get("action"), str(t.get("action"))) for t in trace if t.get("action"))
    cautions = " ".join(packet.get("precautions") or [])
    return f"""<section id="case-{index}" class="case" {'hidden' if index else ''}>
      <div class="meta">연구용 시뮬레이션 · {esc(packet.get('origin',''))}</div>
      <h1>{esc(title)}</h1><p class="meta">활성 알람　{esc(alarms or "없음")}　·　{esc(context.get("mode",""))}</p><div class="signals">{"".join(readings)}</div><div class="status">{esc(LABEL.get(action,action))} <code>{esc(action)}</code></div>
      <p class="reason">{esc(packet.get('reason_ko',''))}</p>
      <div class="engines">검색 <b>{esc(packet.get('requested_retrieval_mode',''))}</b>　판단 <b>{esc(packet.get('backend',''))}</b>　Zoro-1 <b>{esc(packet.get('zoro_status',''))}</b></div>
      {('<h2>운영자 확인 절차</h2><ol>'+instruction+'</ol>') if instruction else ''}
      <p class="meta">{esc(cautions)}</p><p class="path">{esc(path)}</p>
      <h2>절차 근거</h2>{''.join(evidence) or '<p>이 사건에서 안내할 수 있는 유효 조항이 없습니다.</p>'}
      <details><summary>적용 조건과 출처 확인</summary><pre>{json_text({'gate':packet.get('gate'), 'citations':packet.get('citations')})}</pre></details>
      <details><summary>판단과 도구 실행 기록 ({len(trace)})</summary><pre>{json_text(trace)}</pre></details>
      </section>"""


def render(packets, rows, destination):
    if not packets:
        raise ValueError("Provide at least one incident")
    buttons = "".join(f'<button type="button" data-case="{i}" aria-pressed="{str(i==0).lower()}">{esc(TITLES.get(p.get("demo_case"),p.get("demo_case") or "입력 사건"))}</button>' for i,p in enumerate(packets))
    lookup = {row["id"]:row for row in rows}
    sections = "".join(block(p,lookup,i) for i,p in enumerate(packets))
    page = """<!doctype html><html lang="ko"><meta charset="utf-8"><link rel="icon" href="data:,"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ProtoPilot | 알람 대응 기록</title>
<style>*{box-sizing:border-box}body{margin:0;font:16px/1.7 system-ui,sans-serif;color:#192d35;background:#fafcfb}header{padding:17px max(24px,5vw);background:#163941;color:white}header strong{font-size:21px;letter-spacing:.015em}header span{margin-left:18px;font-size:14px;color:#b9d4d5}main{display:grid;grid-template-columns:238px minmax(0,1fr);gap:32px;max-width:1220px;margin:40px auto;padding:0 25px}nav{border-right:1px solid #cfdbd9;padding-right:20px}.signals{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}.signals span{border:1px solid #cfdbd9;padding:3px 9px;font-size:14px}.signals b{color:#315e63;font-size:12px}.path{font-weight:650;color:#385455}nav h2{margin:0 0 12px}button{display:block;width:100%;text-align:left;cursor:pointer;border:0;border-radius:5px;background:transparent;color:inherit;padding:11px 12px;margin:3px 0;font:inherit}button:hover{background:#e7efec}button[aria-pressed=true]{background:#d7e9e4;color:#104c42;font-weight:700}.case{max-width:850px}h1{font-size:clamp(25px,4vw,37px);line-height:1.24;margin:9px 0 20px}h2{font-size:18px;margin-top:28px;color:#23454b}.status{display:inline-flex;align-items:center;gap:14px;font-weight:750;color:#164e45;border-bottom:3px solid #639f92}.reason{font-size:18px}.meta,.engines{font-size:13px;color:#486367}.engines{border-block:1px solid #d5dedb;padding:11px 0}article{border-left:3px solid #94b9b1;padding:6px 16px;margin:14px 0;background:#f0f5f2}article p{margin:4px 0}[lang=en]{font-size:14px;color:#415861}summary{cursor:pointer;font-weight:620}details{padding:13px 0;border-top:1px solid #d5dedb}pre{overflow:auto;max-height:460px;font:12px/1.55 ui-monospace,monospace;background:#eef3f1;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere}li{margin-bottom:5px}code{font-size:12px}.case[hidden]{display:none}@media(max-width:760px){main{display:block;margin-top:18px}nav{border:0;border-bottom:1px solid #cfdbd9;margin-bottom:25px;padding:0 0 14px}button{display:inline-block;width:auto;font-size:14px}} </style>
<header><strong>ProtoPilot</strong><span>알람 대응 기록</span></header><main><nav aria-label="사건 선택"><h2>시연 사건</h2>""" + buttons + "</nav><div>"+sections+"""</div></main><script>
document.querySelectorAll('[data-case]').forEach(button=>button.addEventListener('click',()=>{const k=button.dataset.case;document.querySelectorAll('.case').forEach(s=>s.hidden=s.id!=='case-'+k);document.querySelectorAll('[data-case]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));}));</script></html>"""
    destination.write_text("\n".join(line.rstrip() for line in page.splitlines())+"\n",encoding="utf-8")


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("incidents",type=Path,nargs="+")
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    rows=json.loads((Path(__file__).resolve().parent/"data/corpus.json").read_text())
    render([json.loads(f.read_text()) for f in args.incidents],rows,args.output)
