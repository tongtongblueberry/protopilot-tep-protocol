# TEP 사례와 절차

팀이 작성한 시뮬레이션 절차 `tep-rag-demo.1`의 23개 사례와 영어·한국어 조항 89개를 담았다. 각 사례에는 적용 카드와 판본, 처음 볼 수 있는 관측, 추가로 요청할 계기나 승인 기록, 판단 규칙, 대응 선택과 종료 조건이 있다. [통합 프로토콜](tep_protocol.json) · [파일 해시](snapshot.json)

| 계열 | 사례 수 | 예시 |
| --- | ---: | --- |
| 원료 공급·조성·온도 | 7 | A 유량 급감, 혼합 원료 조성 변화, 공급 온도 확인 |
| 반응기·응축기 냉각 및 밸브 | 4 | 냉각수 조건, 밸브 명령과 실제 유량의 관계 |
| 반응·제품 품질·설비·계기 | 8 | 분석값 시차, 스트리퍼 열공급, 계기 품질 |
| 계획된 운전 변경 | 4 | 생산량·조성·압력 목표, 운전점 전환 |

`tep_protocol.json`의 `sources`는 각 조항에 사용한 문헌과 고정판 구현을 가리킨다. `coverage`에는 원래 외란과 확장 사례의 대응, 관측 가능성과 미확인 조건이 있다. 판단 기준과 승인 기록은 연구 시연용이다.

## 절차 재생

저장소 최상위에서 다음 명령을 실행한다. 재생기는 Python 표준 라이브러리를 사용한다.

```sh
python3 simulator/protocols/verify_protocol.py
python3 simulator/protocols/verify_protocol.py --demo
python3 simulator/protocols/verify_protocol.py --case FD-A-FLOW --json
python3 simulator/protocols/verify_protocol.py --case HX-CASE-R01 --variant wrong-revision
python3 simulator/protocols/verify_protocol.py --case SP-RATE --variant close-without-signoff
```

기록된 시작 장면 23개는 모두 추가 정보를 요청한다. 지정한 후속 관측을 공개하면 21개는 조항 제시 조건을 충족하고, 2개는 운영자 검토로 이어진다. 구판·관측 조회 실패·시효 초과·불량 품질·승인 또는 종료 확인 누락도 검사한다. 재생기는 카탈로그의 규칙과 지정된 후보 조항을 사용한다. 신경망 검색의 성능은 아래의 별도 검색 실행에서 측정했다. [규칙 재생 검사 결과](../../examples/benchmarks/protocol-verification.json)

## RAG 검색

[설치 안내](../../rag/README.md)에 따라 RAG 환경을 준비한 뒤, 저장소 최상위에서 실행한다.

```sh
rag/.venv/bin/python rag/search_cli.py \
  "Which observations verify the reactor cooling water inlet condition and actual flow?" \
  --corpus simulator/protocols/rag_corpus.json --mode bm25 --limit 5

rag/.venv/bin/python rag/benchmark_catalog.py \
  --corpus simulator/protocols/rag_corpus.json \
  --probes examples/benchmarks/catalog-probes.json \
  --output rag/local-output/catalog-cpu.json --device cpu
```

세 검색 경로의 반복 실행에는 RAG 안내에 따라 내려받은 공개 모델이 필요하다. [질의 입력](../../examples/benchmarks/catalog-probes.json)은 각 사례의 공개 관측과 알람으로 만들었다. 정답 카드는 검색 반환 후에 채점에 사용한다. 89개 조항에 대해 후보 20개와 반환 5개를 사용했으며, 결과에는 코퍼스와 질의의 해시가 있다. [실제 CPU 검색 결과](../../examples/benchmarks/catalog-cpu.json)

| 검색 경로 | 해당 카드 1위 | 해당 카드 상위 5위 |
| --- | ---: | ---: |
| BM25 | 11/23 | 19/23 |
| BM25 + BGE-M3 + RRF | 10/23 | 22/23 |
| 결합 검색 + Qwen 재정렬 | 12/23 | 21/23 |

이 수치는 같은 시연 사례의 관측으로 만든 질의에서 얻었다. 별도의 미공개 시험셋과 실제 운전 성과는 후속 평가 대상이다.

- `rag_corpus.json`은 영문 조항과 출처·판본을 제공한다. 새 조항의 종류는 `sop`이다.
- `public_episodes.json`은 23개 사례의 최초 가시 입력이다.
- `eval_cases.json`은 후속에 공개할 값과 기대 결과를 제공한다. 검색 입력은 코퍼스와 각 사건의 공개 관측으로 구성한다.
- `public_episode.json`은 기존 RAG의 RC-01 등록부와 네 계기에 맞춘 별도 연결 예제다.
- 기존 `rag/app.py`는 등록된 RC-01 판본을 검증한다. 새 23개 카드는 그 등록부에서 적용되지 않아 운영자 검토로 이어진다. 새 절차의 판단 규칙은 위의 `verify_protocol.py`로 재생할 수 있다.

물리적인 대응 비교는 [기존 TEP Studio 재생 예제](../../examples/tep-replay/README.md)에 있다. 같은 시작점에서 갈라진 한 실험의 생산량과 최고 압력 차이를 제공한다. 확장 사례의 대응 효과와 실제 Zoro-1 추론은 후속 검증 항목이다.
