# ProtoPilot TEP 운전 프로토콜

반응기 온도가 122.7 °C에서 3분 뒤 123.4 °C로 올랐습니다. 운전원은 첫 화면부터 정상 범위의 압력·액위, 보호 활성, 비개입과 미정지를 확인합니다. 냉각수 입구 온도는 40.0 °C입니다. 화면에 실제 유량이 없다면 같은 관측 구간의 값을 요청합니다. 냉각수가 평소보다 따뜻하고 유량이 줄지 않았으면 기존 생산량 목표 유지와 승인 후 감량을 검토합니다. 유량이 줄었으면 현재 목표와 관측값을 교대 책임자에게 넘깁니다. 마지막에는 정상 복귀를 확인하거나 담당자의 인수를 기록합니다. [첫 사건 따라가기](docs/operator-guide.md#첫-사건-따라가기)

`TEP-DEMO@0.1.0`은 Tennessee Eastman Process(TEP) Studio의 Mode 1 폐루프를 대상으로 작성한 해커톤 훈련 절차입니다. 반응기 냉각 이상을 처음부터 끝까지 다루며, 분리기와 스트리퍼는 후속 관측과 이관을 다룹니다.

- [운전원 안내서](docs/operator-guide.md)에서 조항별 판단과 복귀·인수 조건을 확인할 수 있습니다.
- [조항 데이터](data/protocol.json)에서 적용 조건, 계측 단위, 조치 권한, 공개 출처를 확인할 수 있습니다. 검색과 안내에는 같은 조항 키를 사용합니다.
- [검색용 조항 JSONL](corpus/clauses.jsonl)은 조항당 한 줄씩 내보낸 파일입니다. RAG 색인을 만들 때 이 파일을 입력으로 사용할 수 있습니다.
- 팀의 기존 `rag/search_cli.py --corpus`에는 장비·모드·판본 정보와 여섯 조항을 담은 JSON 배열을 사용합니다. 검색 결과는 읽기 전용 근거(`EVIDENCE_ONLY`)로 표시됩니다. 사건 안내 앱에서 허용되는 조치는 등록된 절차에서 확인합니다. [검색 CLI용 JSON](corpus/rag-reference.json) · [실행 및 호환 안내](docs/rag-compatibility.md)
- [합성 사건 카드](examples/incident-cards.json)로 유량 감소·누락, 판본 불일치, 승인 거절, 보호 개입 등을 대조할 수 있습니다. 진행자는 [판단 시각별 공개 기준](docs/operator-guide.md#사건-진행과-검색-안내)에 따라 계측·상태·승인 기록을 골라 참가자와 RAG에 제공합니다. 카드 ID·제목과 `expected_path`는 진행자가 판정을 확인할 때 씁니다.

## 조항 확인과 검색용 내보내기

저장소를 처음 받는 경우 다음 명령으로 운전 프로토콜 폴더까지 이동합니다.

```sh
git clone https://github.com/tongtongblueberry/protopilot-tep-protocol
cd protopilot-tep-protocol/operating-protocol
```

이미 저장소 최상위 폴더에 있다면 `cd operating-protocol`로 이동합니다. 아래 명령은 `operating-protocol/`에서 Python 3로 실행합니다.

```sh
python3 -m tep_protocol check
python3 -m tep_protocol show RC-02

(
  corpus_tmp=$(mktemp corpus/.clauses.XXXXXX) || exit 1
  trap 'rm -f "$corpus_tmp"' EXIT
  python3 -m tep_protocol export > "$corpus_tmp" &&
    mv -f "$corpus_tmp" corpus/clauses.jsonl
)
```

`check`는 절차집의 구조와 참조를 검사하고, `show`는 조항 하나를 표시합니다. 마지막 블록은 같은 폴더의 임시 파일에 UTF-8 JSON Lines를 내보냅니다. 성공하면 `operating-protocol/corpus/clauses.jsonl`을 한 번에 교체합니다. 실패하면 셸에 오류를 반환하고 기존 파일을 보존합니다. 벡터 색인 생성과 모델 실행은 내보낸 조항을 사용하는 별도 단계입니다. 사건 카드의 기대 경로는 진행자가 판정을 확인할 때 사용합니다. [사건 진행과 검색 안내](docs/operator-guide.md#사건-진행과-검색-안내)
