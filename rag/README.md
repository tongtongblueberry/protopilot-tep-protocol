# ProtoPilot RAG

ProtoPilot은 모의 반응기 알람과 그 시점에 공개된 관측으로 영어 절차를 검색하고, 적용 조건을 확인한 뒤 운영자에게 한국어로 근거를 제시한다. 아래 명령은 저장소의 `rag/`에서 실행한다. Python 3.12에서 시험했다.

## 첫 실행

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r runtime/requirements.txt
python app.py --case compound --mode bm25 --output incident.json
python app.py --case recover --mode bm25 --output follow-up.json
python dashboard.py incident.json follow-up.json --output operator.html
```

`operator.html`을 브라우저로 열면 각 사건의 관측, 절차, 출처와 도구 이력을 볼 수 있다. `recover`에서는 냉각수 출구 관측을 추가 조회한다. `missing`에서는 조회해도 정보가 없어 운영자 검토로 끝난다. `wrong-mode`, `wrong-unit`, `stale`, `other-equipment`, `normal`로 적용 조건을 시험할 수 있다.

## 팀 설계에 따른 검색

```sh
python runtime/fetch_models.py .models
python search_cli.py "Which measurement helps distinguish a cooling inlet condition change from a flow problem?" --mode hybrid_rerank --device cpu
python app.py --case recover --mode hybrid_rerank --device cpu --output follow-up.json
python run_suite.py --device cpu --output local-output
```

실제 체크포인트를 내려받아 파일별 해시를 확인한다. `bm25`는 용어 검색, `hybrid`는 Qdrant가 BM25와 BGE-M3 검색 순위를 RRF로 결합한 결과다. `hybrid_rerank`는 결합 후보를 Qwen3-Reranker-0.6B의 yes/no 확률로 재정렬한다. 준비 시간과 질의 시간은 결과 JSON에 따로 저장한다. 검색 점수는 문서의 적용 권한을 나타내지 않는다.

## 자료와 연결 규격

- 연구용 카드 [RC-01](data/sources/rc01.json)의 한국어 5개 조항을 영어 검색 단위로 가공했다. [코퍼스 생성 코드](data/build_corpus.py)와 [검색 항목](data/corpus.json)에서 번역·문맥·출처를 대조할 수 있다. 기술 참고 항목은 운전 절차를 승인하지 않는다.
- [복합 알람 예제](data/sources/episode.json)는 합성 시험 입력이다. 모델이 보는 태그는 XMEAS(7/8/9/21)이며, 원시 경보 발생·인지·복귀와 관측 시각을 구별한다. `--event`에 사용자 사건을 넣을 때는 JSON에 `snapshot`과 해당 입력에 맞는 `runtime`, 자료 출처 `origin`을 함께 제공한다. `origin`은 `synthetic_fixture` 또는 `tep_replay`이며 `--origin`으로도 지정할 수 있다. 요구하는 계약은 [agent.py](agent.py)에 있다.
- 판단기는 `PROVIDE_SOP`, `REQUEST_INFO`, `RETRIEVE_AGAIN`, `ESCALATE` 네 도구를 사용한다. 기본값은 재현 가능한 규칙이다. 사용 권한이 있는 Zoro 서버가 있으면 `--zoro-endpoint`에 공급자의 `/v1/systemone` 또는 기존 `/v1/decide` URL을 지정한다. 이 모드에서는 관측과 검색 근거를 그 서버로 전송한다. 실제 모델을 사용하지 않은 실행에는 `NOT_RUN`을 기록한다. [공급자 연결 규격](https://huggingface.co/trillionlabs/Zoro-1#using-the-typed-decision-head)
- PDF의 원하는 쪽을 참고 자료로 검색하려면 Poppler의 `pdftotext`를 설치한 뒤 `python ingest_pdf.py --help`를 확인한다. 출력은 쪽·블록·원본 해시가 붙은 `reference` 행이다. `search_cli.py --corpus data/corpus.json 새파일.json`으로 기존 자료와 함께 검색할 수 있다. 운영자 안내에는 등록된 연구 절차만 사용한다.

이 데모는 시뮬레이션을 위한 의사결정 지원이다. 실제 공장의 승인 절차와 장치 제어 기능은 포함하지 않는다.

## 재현 시험

`python -m unittest discover -s tests`로 시점·센서 품질·절차 판본·다음 행동 검사를 실행한다. `python run_suite.py --device cpu --output local-output`은 고정 질문별 순위, 준비 및 질의 시간, 사건 8개의 JSON을 저장한다. 기본 Qdrant는 Python 프로세스 내 메모리 모드다. 개발용 독립 Qdrant 1.16.2 서버를 검증할 때는 `--qdrant-url http://127.0.0.1:6333`을 추가한다. 실행마다 이 실험 전용 컬렉션을 생성하고 종료 시 회수한다.

## SNUCSE SGS에서 반복하기

워크스페이스의 로그인된 kubeconfig 경로, 컨텍스트 이름, 네임스페이스를 각각 `SGS_KUBECONFIG`, `SGS_CONTEXT`, `SGS_NAMESPACE` 환경변수에 설정한다. 다음 명령은 실행에 필요한 파일의 목록과 단발 작업 명세를 로컬에 만든다. GPU 1개를 실제로 요청할 때는 새 출력 경로와 함께 마지막에 `--run`을 붙인다. CPU로 실행하려면 `--gpu 0`과 `--command '["python","runtime/sgs_entry.py","--device","cpu"]'`를 사용한다.

```sh
python runtime/sgs_run.py --source . --allowlist runtime/sgs-allowlist.json \
  --command '["python","runtime/sgs_entry.py"]' --result remote_output/bundle.json \
  --output local-output/sgs-plan --gpu 1 --cpu 4 --memory 16Gi \
  --image docker.io/pytorch/pytorch@sha256:dab81780fd94483b67b4b5679cc0024939b08e48540d39476d284cb29002ed69 \
  --kubeconfig "$SGS_KUBECONFIG" --context "$SGS_CONTEXT" --namespace "$SGS_NAMESPACE" \
  --start-seconds 480 --run-seconds 1200
```

Pod 안에서 공개 모델과 고정 판본의 Qdrant를 임시 공간에 받아 검색한다. 실행기는 이번 작업의 결과를 지정한 로컬 폴더에 받은 뒤 해당 Pod를 회수한다. 작업 원본과 클러스터 로그에는 내부 식별자가 있으므로 공유할 때는 검색 결과 파일을 선택한다. [SGS 단발 GPU 작업 예시](https://sgs-docs.snucse.org/usage/run-workload-examples.html)
