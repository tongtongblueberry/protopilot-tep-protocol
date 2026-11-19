# TEP 관측 기록 재생

이 예제는 같은 초기 상태에서 생산 목표를 유지하거나 0.10 m³/h 낮춘 TEP Studio 실행의 관측 기록을 재생한다. 시뮬레이션 2시간에 목표를 선택한 뒤 8시간까지의 제품 유량을 적분한다. 시각과 두 경로의 반응기 온도·압력·제품 유량을 800개 행에 보존했다. [관측 CSV](visible-trace.csv) · [생성 출처와 해시](provenance.json)

| 목표 선택 뒤 6시간 | 기존 목표 유지 | 목표를 0.10 m³/h 낮춤 |
| --- | ---: | ---: |
| 반응기 최고 온도 | 120.477228°C | 120.476948°C |
| 반응기 최고 압력 | 2712.818014 kPa(g) | 2712.018568 kPa(g) |
| 제품 혼합물 누적 부피 | 137.150965 m³ | 136.797054 m³ |

부피 차이는 −353.910901 L이다. 전 구간의 저장 표본에서 원논문 정상 운전 상한 150°C와 2895 kPa(g)를 넘은 횟수는 두 경로 모두 0회다. 이 기준은 원논문의 벤치마크 운전 범위를 뜻한다. CSV의 실제 경보 이력과 품질 플래그는 `UNKNOWN`이며 표본 사이의 최대값도 알 수 없다. 이 한 쌍의 결과만으로 최적 조치를 정할 수 없다. [계산 결과](comparison.json) · [Downs·Vogel 원논문 p.249, 표 4·6](https://users.abo.fi/khaggblo/RS/Downs.pdf#page=5)

저장소의 `examples/tep-replay/`에 이 폴더를 두고 루트에서 실행한다. 수치 재생에는 Python 표준 라이브러리를 사용한다.

```sh
python3 rag/tep_replay.py examples/tep-replay/visible-trace.csv \
  --output replay-comparison.json \
  --operator-output replay-operator.json --arm keep --as-of-h 2
```

`replay-comparison.json`은 전체 비교 결과를 담는다. `replay-operator.json`에는 선택 경로에서 기준 시점까지 관측된 온도·압력과 영문 검색 문장이 들어간다. 시간 비교의 허용 오차는 부동소수점 계산을 위한 10⁻⁹ 시간이다. 반응기 수위와 냉각수 출구 온도는 이 CSV에 없어 누락으로 표시한다. 실제 절차 적용에는 기록된 경보·관측 품질·공정 및 문서 버전 검증이 더 필요하다. [2시간 운영자 입력 예](operator-view.json)

생성된 문장을 실제 RAG에 검색한 예는 `RC01-S3`을 1위로 반환했다. 이 파일에는 검색 결과와 검색 방식이 기록돼 있다. [검색 실행 결과](rag-search.json)

```sh
rag/.venv/bin/python - <<'PY'
import json
import subprocess
view = json.load(open('replay-operator.json', encoding='utf-8'))
subprocess.run(['rag/.venv/bin/python', 'rag/search_cli.py', view['query_en'], '--mode', 'bm25'], check=True)
PY
```

원래 시뮬레이션의 재생성 코드도 함께 제공한다. 한 쌍과 유지 경로 반복을 총 세 번 새로 실행했을 때 기존 원본 CSV의 SHA-256이 재현됐고, 목표 선택 이전 200행의 160개 열도 일치했다. 이 검사는 기존에 설치된 고정판 환경에서 수행했다. [재생성 코드](regenerate_trace.py) · [실행 검증](regeneration.json)

```sh
python3.13 -m venv .tep-venv
.tep-venv/bin/python -m pip install 'numpy==2.2.6' 'scipy==1.15.3' 'pandas==2.3.3' \
  'tep-studio @ git+https://github.com/khalidlabs/tep-studio.git@852f0c69ab974540750d61bd1dbfafd56bfd80bb'
.tep-venv/bin/python examples/tep-replay/regenerate_trace.py --csv paired-evaluator-trace.csv > paired-evaluator-results.json
python3 rag/tep_replay.py paired-evaluator-trace.csv --output replay-comparison.json \
  --operator-output replay-operator.json --arm keep --as-of-h 2
```

새 환경 설치에는 인터넷과 C 확장 빌드 도구가 필요하다. 새 설치 명령 자체의 재검증은 남아 있다. 생성기의 전체 실험 설정과 뒤 시점 기록은 평가용이다. RAG에는 `--operator-output`으로 만든 시점별 입력의 `query_en`을 전달한다.

관측값은 팀 실행에서 생성한 수치이고, 재생성과 재생 코드는 팀 작성 코드다. TEP Studio의 계기 정의와 라이선스, 원래 모델 저자 표기는 아래 원문에서 확인할 수 있다. [고정판 계기 정의](https://github.com/khalidlabs/tep-studio/blob/852f0c69ab974540750d61bd1dbfafd56bfd80bb/src/tep_studio/simulation/schema.py#L407-L429) · [TEP Studio 및 포함 모델 저작권 고지](https://github.com/khalidlabs/tep-studio/blob/852f0c69ab974540750d61bd1dbfafd56bfd80bb/LICENSE)
