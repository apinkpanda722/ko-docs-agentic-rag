# Step 14: test-run

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/eval/m5_runner.py`, `src/kodocs/eval/m5_dev.py`(`verify_config`), `src/kodocs/eval/m5_analysis.py`, `src/kodocs/eval/m5_inputs.py`
- `src/kodocs/retrieval/lookup.py`(`extract_query_identifiers`, `authorized_lookup_id`), `src/kodocs/retrieval/index_build.py`
- `data/manifest/m5_config.json` 형식(step 12), `data/manifest/m5_prior_versions.json`, `data/manifest/m5_eval_inputs.json`
- `src/kodocs/ingest/manifest.py` — `latest_records`(append-only 읽기 규칙)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

test 1회 실행기와 재실행 잠금을 만든다. 신규 파일: `src/kodocs/eval/m5_test.py`, `tests/eval/test_m5_test.py`.

### CLI

`python -m kodocs.eval.m5_test [--rerun-reason "<사유>"] [--embedder bge-m3|fake] [--out-root PATH]`

### 실행 전 검사 (하나라도 실패하면 아무것도 실행하지 않고 비정상 종료)

1. `data/manifest/m5_config.json`이 있고 `verify_config`가 통과(해시·`m5_eval_inputs.json`·`m5_dev.json` 해시 일치).
2. `m5_index.json`의 인덱스 4개가 config가 기록한 스키마 해시·임베더 설정 해시와 일치하고 색인 실패 수가 0.
3. `m5_eval_inputs.json`의 권한 전제 검사 실패(`visibility.failures`: 보이는 정답 0개 문항·코퍼스에 없는 문서)·구간 매핑 실패가 모두 0건. (제외 쌍 `excluded_pairs`는 실패가 아니라 정의된 제외다.)
4. **잠금**: `data/manifest/m5_test_runs.jsonl`(append-only)에 이전 실행 기록이 있으면 `--rerun-reason`이 비어 있지 않아야 한다. 없으면 "이전 실행이 있습니다. 재실행은 --rerun-reason이 필요합니다"라고 출력하고 종료 코드 2. 이전 기록이 없는데 `--rerun-reason`이 주어지면 무시하지 말고 오류로 알린다(첫 실행은 사유가 없다).

### 실행 내용 (한 번에 전부)

- **문항 집합**: 골드 test 검색 지표 대상 21, 실버 test 65(방향 확인), 실제 골드 검색 지표 대상 19(dev 6 + test 13; 파서 탐색 비교용). dev 문항은 파서 비교의 실제 19에만 쓰이며 튜닝에는 쓰이지 않는다.
- **조합**: 확정 설정(`m5_config.json`)으로 `all_combo_configs`의 14조합(composite 조건) + 파서 탐색 비교용 조건(`pypdf`·`upstage`, 구조 청킹, 최종 hybrid 설정 각 1조합)을 실행한다. 질의 임베딩은 캐시를 쓴다.
- **H4용 식별자 조회**: 골드 test identifier 4·실버 test identifier 15 문항에서 `extract_query_identifiers`(서비스 이름 목록은 `data/synthetic/ledger.sqlite`의 `services`에서 읽는다)로 식별자를 뽑아 `authorized_lookup_id(..., rank_query=질문)`로 조회하고, 1위 문서가 rel ≥ 1인지를 `lookup_top1[qid]`로 만든다. 식별자를 못 뽑았거나 결과가 비면 미적중(False)이다. 조회는 최종 구성(composite·구조) 인덱스에서 한다.
- **분석**: `m5_analysis`의 함수들을 호출해 결과 객체를 만든다(사다리, 제거, 전체 조합, H1·H2·H3·H4·H7, H2 분해, 코퍼스 v2 규칙, 개정 전/현행 순위 통계, 파서 탐색 비교, 실버 방향).
- **환경 기록(FP-044)**: 실행 시각(Asia/Seoul), 코드 커밋 해시, `config_hash`, 임베더 설정 해시·장치·`init_s`, OpenSearch 버전(`GET /`)·힙(`docker-compose.yml` 값), 플랫폼 문자열, 부트스트랩 시드·횟수, 문항 수와 무엇을 세는지.

### 산출·기록

- 결과: `data/manifest/m5_test_results/run-{seq:02d}.json`(커밋 대상, `seq`는 1부터). 문항별 원시 결과(문서 순위 등)는 `data/interim/m5/run-{seq:02d}/`(비커밋).
- 실행 기록: `data/manifest/m5_test_runs.jsonl`에 한 줄 추가(키 = `seq`): `seq`, `run_id`, `ran_at`, `config_hash`, `rerun_reason`(첫 실행은 null), `previous_result_sha256`(직전 실행의 결과 파일 SHA-256, 첫 실행은 null), `result_sha256`, `result_path`. 파일은 append-only이고 읽을 때는 `latest_records`(key=`seq`)로 읽는다(FP-032). 재실행 사유·날짜·이전 결과 해시가 누적된다.
- **막힌 채로 두지 않는다**: 재실행 경로(`--rerun-reason`)를 끝까지 구현하고 테스트한다. 재실행 결과도 새 `run-NN.json`으로 남고 이전 결과 파일은 수정하지 않는다.

### 테스트 (가짜 러너·가짜 분석 결과 주입)

첫 실행 성공 후 기록이 남음, 두 번째 실행이 사유 없이 거부(종료 코드 2)됨, `--rerun-reason`으로 재실행이 허용되고 `previous_result_sha256`가 첫 결과의 해시와 같음, 첫 실행에 사유를 주면 오류, 실행 전 검사 각 항목(config 해시 변조·색인 실패·권한 전제 실패) 실패 시 검색이 한 번도 호출되지 않음, 결과 파일이 이미 있으면 덮어쓰지 않음. `make verify`는 실제 OpenSearch·모델 없이 통과해야 한다.

## Acceptance Criteria

```bash
make verify
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
   - 성공한 실행만 잠금을 소모하고 재실행 경로가 테스트돼 있는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 네트워크 요청·다운로드·패키지 설치를 하지 마라. 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 필요해 보이면 `blocked`로 보고한다.
- LLM·외부 API를 호출하거나 `anthropic`·`openai`·`google.genai`·`kodocs.llm`을 import하지 마라. 이유: M5는 결정론 검색 실험 phase이며 LLM 호출이 생기면 멈추고 보고하는 조건이다.
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `src/kodocs/retrieval/`에서 `langchain`·`langgraph`·`kodocs.agent`·`kodocs.mcp_server`·`kodocs.ui`를 (상대 import 포함) import하지 마라. 이유: 검색은 측정 대상이라 추상화 없이 OpenSearch·SQLite를 직접 호출한다(ADR-009).
- 기존 테스트를 깨뜨리지 마라
- test 결과를 본 뒤 설정·분석 정의를 바꿔 다시 실행하는 우회 경로(락 제거·`--force`·기록 삭제)를 만들지 마라. 이유: 재실행은 `--rerun-reason`으로만 허용되고 사유·이전 결과 해시가 누적돼야 한다(ADR-028 11번).
- 이전 `run-NN.json`을 수정·덮어쓰지 마라. 이유: 결과 문서는 첫 실행과 재실행을 모두 보고한다.
- 실행 전 검사를 경고로만 처리하고 계속 진행하지 마라. 이유: 검사 실패는 test 1회 기회를 소모할 수 있다. 검사가 통과하기 전에는 어떤 검색도 하지 않는다.
- 문항 하나의 검색 오류를 삼키고 나머지로 지표를 계산하지 마라. 이유: 부분 결과가 분모를 조용히 바꾼다(FP-021). 오류가 나면 실행 전체를 실패로 끝내고 기록에 `failed`로 남기되 **잠금은 성공한 실행만 소모한다**(실패한 실행은 결과 파일을 만들지 않으며 다음 실행에 사유가 필요 없다).
- 실제 BGE-M3·OpenSearch로 실행하지 마라. 이유: 실측은 구현이 끝난 뒤 사용자가 실행한다(`--embedder fake`는 로컬 시험용이며 fake 실행은 잠금 기록을 남기지 않도록 `--out-root`가 있을 때 격리한다).
