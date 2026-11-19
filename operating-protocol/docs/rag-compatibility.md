# ProtoPilot에서 TEP 조항 검색

TEP-DEMO 0.1.0의 여섯 조항을 기존 ProtoPilot 검색에 참고 자료로 추가할 수 있습니다. [운전 프로토콜](../data/protocol.json)의 조항 번호와 [조항별 JSONL](../corpus/clauses.jsonl)의 판본을 유지해 [ProtoPilot 검색용 JSON](../corpus/rag-reference.json)을 만들었습니다.

저장소의 `rag/` 폴더에서 다음 명령을 실행합니다. 실행 환경은 [ProtoPilot RAG 안내서](../../rag/README.md)에서 확인할 수 있습니다.

```sh
python search_cli.py "Which observations distinguish a warmer reactor cooling-water inlet from lower water flow?" \
  --corpus data/corpus.json ../operating-protocol/corpus/rag-reference.json \
  --equipment TEP_REACTOR --mode bm25
```

검색 결과의 `hits[].passage`에는 한국어 조항, 판본, 원본 위치와 각 기술 근거가 포함됩니다. 장비 필터는 조항의 첫 장비에 적용됩니다. 여러 설비에 걸친 근거가 필요하면 `--equipment` 없이 검색합니다. 검색 결과의 결정 필드는 `EVIDENCE_ONLY`입니다. [검색 명령의 입력과 출력](https://github.com/tongtongblueberry/protopilot-tep-protocol/blob/da186dfb128257b0585dfceb63c9f06cbe91ccc7/rag/search_cli.py#L11-L32)

사건 안내 앱은 등록된 RC-01 절차와 기록의 해시를 대조합니다. 새 여섯 조항을 사건 안내에 사용하려면 등록 판본과 적용 조건, 절차 안내·정보 요청·재검색·이관 흐름을 함께 검증해야 합니다. [앱에서 읽는 코퍼스](https://github.com/tongtongblueberry/protopilot-tep-protocol/blob/da186dfb128257b0585dfceb63c9f06cbe91ccc7/rag/app.py#L73-L96) · [등록 조항 검증](https://github.com/tongtongblueberry/protopilot-tep-protocol/blob/da186dfb128257b0585dfceb63c9f06cbe91ccc7/rag/agent.py#L246-L260)

검색용 JSON 한 행은 조항별 JSONL의 판본 포함 키, 영문 검색어, 한국어 본문과 관측 태그를 보존합니다. `source.locator`는 원본 JSON의 조항 위치를 가리키며 `source.payload_sha256`은 그 판본 파일의 바이트를 확인할 때 사용합니다. 원본 조항을 개정하면 두 검색 파일을 함께 갱신합니다.
