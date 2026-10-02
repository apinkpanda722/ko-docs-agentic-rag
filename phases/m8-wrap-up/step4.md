# Step 4: readme

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `src/kodocs/eval/m7_report.py`·`m5_report.py`·`m6_report.py` — 결과 표를 manifest에서 만드는 함수들(H1~H7 판정 표, H5·H6 표, 지연 표, 비용 표를 **재사용**한다. 값을 새로 계산하거나 손으로 옮기지 않는다)
- `docs/results/2026-10-02-m7-answer-eval.md`(생성 영역의 '핵심 결과 요약') — README에 실을 표의 원형
- `docs/results/2026-09-30-m5-retrieval.md`('검색 결과' 사다리 표)·`data/manifest/m5_test_results/run-01.json`·`m5_config.json`
- `data/manifest/m7_analysis.json`·`m7_security_results.json`·`m7_latency.json`·`m7_failures.json`·`costs.jsonl`·`m7_grade_summary.json`·`m7_human_grades.json` — 수치 출처 필드 이름(FP-011: 실제 파일을 읽어 확인)
- `docs/results/2026-10-03-m8-wrap-up.md`(step 2)·`data/manifest/m8_parse_error.json`·`data/manifest/m8_audit.json` — 한계·정정 수치 출처
- `docs/results/hypotheses.md` 'M8 판정 규칙'(특히 M8-D1 표시 규칙)·`src/kodocs/eval/m8_rules.py`(step 0)
- `docs/PRD.md`·`docs/ARCHITECTURE.md`(데이터 흐름)·`docs/THREAT_MODEL.md` — 문제·접근·아키텍처 그림의 근거
- `docs/ADR.md`·`docs/ADR-INDEX.md`(step 2) — 링크 대상
- `Makefile`·`AGENTS.md` 명령어 절 — 재현 방법 순서
- `src/kodocs/ui/`(step 3) — GUI 설명

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 이 phase(m8-wrap-up) 개요

PRD M8: 공개 가능한 마무리(공개 전환 점검, ADR 정리, Streamlit GUI, README)와 리랭커 실험(H8). 속도 우선이라 필수 항목(step 0~4)을 먼저 끝내 언제든 공개할 수 있는 상태를 만들고, 리랭커(step 5~7)는 그 뒤에 붙인다.

| step | name | 내용 |
|---|---|---|
| 0 | `m8-prereg` | H8 사전 등록(hypotheses.md 'M8 판정 규칙', ADR-031)과 규칙 절 해시 모듈 |
| 1 | `public-audit` | 공개 전환 점검 스캐너(`make m8-audit`·`make m8-audit-gh`)와 점검 문서 |
| 2 | `docs-cleanup` | ADR 상태·색인, M7 정정(패키지명·parse_error 오프라인 분류), 별표 한계를 M8 결과 문서로 |
| 3 | `streamlit-ui` | Streamlit 데모 GUI(질의 화면·결과표 뷰어), answer()만 호출 |
| 4 | `readme` | README 생성기(`make readme`)와 README.md. H8은 결과가 없으면 '미실행' |
| 5 | `rerank-core` | 리랭커 상수·CrossEncoder 래퍼·순서 재배열 순수 함수·`make rerank-models` |
| 6 | `rerank-dev` | 후보·점수 캐시, max_length 확정, dev 격자, freeze |
| 7 | `rerank-test` | test 1회 잠금, 대조군 일치 검사, 짝 비교 부트스트랩, 지연, 결과 문서 |

고정 전제(모든 step 공통):
- M5~M7 확정 결과와 고정 설정(m5 `config_hash` `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`, `data/manifest/m6_config.json`, `data/manifest/m7_config.json`)은 소급 변경하지 않는다. M8 실험은 새 구성으로 따로 기록하고 기존 판정과 섞지 않는다.
- M8 결과 문서 파일명 날짜는 고정값 `2026-10-03`이다: `docs/results/2026-10-03-m8-wrap-up.md`(step 2), `2026-10-03-m8-public-readiness.md`(step 1), `2026-10-03-m8-rerank.md`(step 7).
- 이 phase의 step에서는 실제 API 호출, 모델·데이터 다운로드, 패키지 설치, `.env` 열람을 하지 않는다. 사용자가 직접 실행하는 명령은 step 파일이 아니라 사용자가 실행한다.

## 작업

면접관이 5분 안에 읽는 한국어 README를 만든다. 수치는 모두 manifest에서 코드가 생성하고 손으로 옮기지 않는다. 리랭커(H8) 결과는 아직 없을 수 있으므로 **세 상태**로 처리한다. README는 `README.md`(저장소 루트), 생성기는 `src/kodocs/eval/m8_readme.py`, 타깃은 `make readme`다.

### 1. 계약 모듈 `src/kodocs/eval/m8_contract.py` (step 7이 이 계약대로 결과를 쓴다)
H8 결과의 경로·필수 필드·검증을 한 곳에 정의한다. step 7의 `m8_test.py`와 이 README 생성기가 같은 상수를 import한다.
- 경로 상수: `FREEZE_PATH = data/manifest/m8_config.json`, `RESULTS_DIR = data/manifest/m8_test_results/`, `RESULT_PATH = RESULTS_DIR/run-01.json`, `RUNS_PATH = data/manifest/m8_test_runs.jsonl`, `LATENCY_PATH = data/manifest/m8_latency.json`.
- `required_result_keys` 및 `validate_result(payload) -> None`(없는 키 → `ContractError`): 최소 키는 `h8`(verdict 문구, `diff`, `ci_low`, `ci_high`, `n`), `gold`·`silver`(대조군·재정렬 평균 NDCG@10·Recall@10), `harm`(순위가 나빠진 문항 수: gold·silver), `selected`(depth, fusion), `max_length`, `truncated_ratio`(test), `device`, `raw`(`sha256`, `rows`), `config_hash`다. 키 이름은 이 모듈이 정의하는 것이 계약이며 step 7은 이를 따른다. 지연(`m8_latency.json`)의 필수 키(`rerank_s` P50·P95·max, `conservative_sum_s`, `margin_to_5s`, `simple_p95_m7`)도 같이 정의한다.
- **H8 상태 판정** `load_h8_state(root=Path(".")) -> H8State`:
  1. **미실행**: (a) `RESULTS_DIR`이 없고 `RUNS_PATH`도 없다(freeze 파일 `m8_config.json`만 있어도 '미실행'이다). 이 상태는 오류가 아니다.
  2. **결과**: `RESULT_PATH`(재실행이 있으면 `RUNS_PATH`의 최신 completed 기록이 가리키는 `run-NN.json`; 첫 실행 값도 병기)가 있고 검증을 통과한다. 검증은 두 층이다. (가) **커밋 쪽 검증(필수)**: `validate_result` 통과 / `RUNS_PATH`의 최신 completed 기록의 `result_sha256`이 `RESULT_PATH`의 SHA-256과 같다 / 결과의 `raw.sha256`·`raw.rows`가 그 기록의 값과 같고 형식이 올바르다 / `config_hash`가 `m8_config.json`의 해시와 같다. (나) **로컬 원본 검증(원본이 있을 때만)**: 비커밋 원본 `data/interim/m8/test/run-01/runs.jsonl`이 존재하면 SHA-256·행 수가 기록과 같아야 한다(`m7_rawhash.verify_fingerprint` 방식). 원본이 로컬에 없으면(다른 clone) 이 층은 `local_raw_checked=false`로 표시하고 실패로 세지 않는다 — 단 README에 "원본 해시 대조 생략(로컬 원본 없음)"을 표시한다.
  3. **오류(멈춤)**: `RESULTS_DIR`이 존재하거나 `RUNS_PATH`에 기록이 있는데 `RESULT_PATH`가 없거나, 있는데 검증에 실패한다(`H8ResultError`). `make readme`는 이 경우 0이 아닌 종료 코드와 원인(어떤 검증이 실패했는지, 값은 해시 앞 12자까지만)으로 멈춘다. "미실행"으로 숨기지 않는다.
- 테스트 `tests/eval/test_m8_contract.py`: **세 경우를 각각** 테스트한다 — (1) 아무 파일도 없음 → 미실행, freeze만 있고 test 결과 없음 → 미실행 (2) 결과 파일과 기록이 일치 → 결과 표시(원본 있음/없음 두 변형) (3) 디렉터리는 있는데 결과 파일 없음 → 오류, 결과 파일은 있는데 해시 불일치 → 오류, 기록은 있는데 결과 없음 → 오류. 로컬 원본이 있고 해시가 다르면 오류.

### 2. README 구조 (`README.md`)
`<!-- BEGIN generated -->`~`<!-- END generated -->` 영역은 `make readme`가 쓰고, 영역 밖은 손으로 쓴 고정 문장이다. `make readme`는 영역 밖을 바꾸지 않는다. 순서와 내용:
1. **한 문장 요약과 문제·접근**(손으로): 한국어 사내 문서에서 질의 유형별로 도구를 고르는 Agentic RAG와 검색·답변·보안 평가 하네스. 주 서사는 검색 품질을 측정으로 개선한 것(사다리), 보조 서사는 에이전트가 언제 필요한지·권한을 새지 않게 한 것이다(`docs/PRD.md`의 문장을 근거로). 한 번에 한 변수만 바꾸고 결과 전에 가설을 사전 등록했다는 점.
2. **아키텍처 그림**: Mermaid 코드 블록(`flowchart`)으로 `docs/ARCHITECTURE.md`의 데이터 흐름을 그린다(적재 → OpenSearch/SQLite → `tools` → `agent`·`mcp_server`, `retrieval.authorized_*` 단일 인가 지점, 사용자 컨텍스트는 서버가 주입). 그림에 수치를 넣지 않는다.
3. **핵심 결과**(생성): (가) M5 검색 사다리(골드 test 21·실버 test 65, 단계별 Recall@10·NDCG@10과 이전 대비 Δ와 95% CI) (나) **H1~H8 판정 표**: H1~H7은 기존 결과(M7 결과 문서 '핵심 결과 요약'을 만드는 함수·manifest)에서, H8은 `load_h8_state` 결과(결과/미실행) (다) H5 유형별·보조 비교와 H6 경로·시스템별 표 (라) 지연(단순 RAG·Agentic, P50/P95/TTFT)과 M7 P95 목표 판정, 리랭커 지연은 H8 결과가 있을 때만 (마) 비용(운영·평가·지연 비용 구분). 표 아래에 각 표의 출처(결과 문서 링크)를 쓴다. H8이 "미실행"이면 "리랭커 실험(H8)은 미실행"이라고만 쓰고 가짜 수치를 만들지 않는다.
4. **재현 방법**(손으로 + 생성): 환경(Python 3.12, uv, Docker/OpenSearch), `make verify`는 API 키 없이 통과한다는 점, 마일스톤별 명령 순서와 어떤 명령이 실제 API 호출·과금인지·사용자 실행인지 표시(`AGENTS.md` 명령어 절과 어긋나지 않게, 명령 목록은 `Makefile`의 타깃 이름이 실제로 존재하는지 테스트로 검사). 법제처·ALIO 원문은 저장소에 없고 사용자가 수집한다는 점(원문 재배포 금지, ADR-002).
5. **한계**(손으로 + 생성 수치): 합성 문항·단일 시드·표본 크기, H2·H3·H4 판정 보류, **n-gram 분석기는 M5에서 제외했다(사전 등록 가설에 걸려 있지 않음, ADR-028)** — 근거 링크는 `docs/ADR.md`의 ADR-028과 M5 결과 문서 `docs/results/2026-09-30-m5-retrieval.md`('범위와 제외 사유' 절)이며 수치는 쓰지 않는다, 별표 조립 결함(영향 별표·문항 수는 `m8-wrap-up` 결과에서 생성), 정적 토큰·OAuth 미구현, 사람 채점 1인 등 M7 결과 문서 한계 절과 일치하게. 수치는 생성한다.
6. **사후 정정 공개**(생성 + 링크): M5 정답 집합 변경(test 결과 전), M6 dev 1차 실행 무효, M7 채점 캐시 키 충돌, c2 식별자 고유성 정정(정정 전 3건·정정 후 0건 — `m7_security_results.json` history에서 생성), 지연 환경 기록 패키지명 정정, 스트리밍 parse_error. 각 항목은 정정 사유와 원문 링크(hypotheses.md 변경 기록, 결과 문서)를 단다.
7. **데모**: 주 데모 = Claude Code + MCP 장면, 보조 = Streamlit GUI 스크린샷. 이미지 파일(`docs/images/demo-claude-code-mcp.gif`, `docs/images/demo-streamlit.png`)이 있으면 임베드하고, 없으면 자리 표시 문구가 아니라 "촬영 예정"이라고 쓴다(생성기가 파일 존재 여부로 결정). 촬영은 사용자가 한다.
8. **문서 링크**: PRD·ARCHITECTURE·ADR·ADR-INDEX·EVAL·THREAT_MODEL·결과 문서 목록.
- LICENSE 문구는 README에 쓰지 않는다(사용자가 공개 직전에 결정).
- README 전체는 400줄 이하로 유지한다(5분 읽기). 초과하면 표를 요약하고 링크한다.

### 3. 수치 규칙 (손으로 옮기지 않음)
- 생성 영역의 모든 수치는 manifest 값에서 코드가 만든다. 영역 **밖** 손으로 쓴 문장에는 결과 수치(소수점·퍼센트·`κ`·`P95`·`NDCG`·`Recall` 뒤 숫자)를 쓰지 않는다. 테스트가 이를 검사한다(버전 표기 `Python 3.12`·`OpenSearch 3.x` 같은 것은 허용 — 허용 규칙을 테스트 안에 명시한다).
- 기존 보고 함수를 재사용한다(`m7_report`의 판정 표 등). 같은 수치를 두 곳에서 다르게 계산하지 않는다.
- `make readme`는 네트워크·API 없이 로컬 manifest만 읽는다.

### 4. Makefile·AGENTS.md
- `.PHONY`에 `readme`를 추가하고 `readme:`(`uv run python -m kodocs.eval.m8_readme`)를 만든다. `AGENTS.md` `## 명령어`에 `make readme  # README 생성 영역 갱신(manifest에서 생성, H8은 결과 없으면 '미실행', 결과 흔적이 있는데 검증 실패면 오류) — 네트워크 없음`을 추가한다.

### 5. 테스트 (`tests/eval/test_m8_readme.py`)
- fixture manifest로 생성한 README 생성 영역의 핵심 수치(H5 차이·CI, κ, 단순 RAG P95, H6 누설 건수, 사다리 행 하나)가 manifest 값과 문자열로 일치한다.
- 실제 저장소의 `README.md` 생성 영역이 `make readme` 결과와 같다(최신 여부) — 이후 step 7에서 H8 결과가 생기면 사용자가 `make readme`를 실행하지 않았을 때 이 테스트가 실패해야 한다.
- H8 세 상태가 README에 올바르게 반영된다(미실행 문구 / 결과 표 / 오류로 멈춤).
- 이미지 파일 유무에 따른 "촬영 예정" 표시.
- 한계 절에 n-gram 제외 항목과 ADR-028·M5 결과 문서 링크가 있음, 손으로 쓴 영역에 결과 수치가 없음, 400줄 이하, Mermaid 블록 존재, `Makefile` 타깃 참조 존재.
- 마지막에 `make readme`를 실행해 README를 만들고, 한 번 더 실행해 diff가 없는지(멱등) 확인한다.

## Acceptance Criteria

```bash
make readme   # README 생성(오프라인)
make readme   # 한 번 더 실행한 뒤 `git diff --stat README.md`가 첫 실행 결과와 같은지(멱등) 직접 확인해 보고
make verify   # ruff + pytest 통과
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조와 계층 경계(`retrieval`은 langchain·langgraph·tools·agent·mcp_server·ui를 import하지 않는다 등)를 따르는가?
   - ADR 기술 스택을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙을 위반하지 않았는가?
   - 이 step의 `## 금지사항`을 모두 지켰는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(생성·수정 파일, 핵심 결정, 다음 step이 알아야 할 값)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 수동 설정, 코드에서 확인할 수 없는 값 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- README 생성 영역의 수치를 손으로 쓰거나 결과 문서에서 복사해 붙이지 마라. 이유: 손으로 옮기면 manifest와 어긋난다. 코드가 manifest에서 생성하고 테스트가 대조한다.
- H8 결과가 없는데 가짜·자리표시 수치를 만들지 마라. 이유: 미실행은 '미실행'이다(M8-D1). 결과 디렉터리·실행 기록이 있는데 결과 파일이 없거나 해시 검증에 실패하면 '미실행'으로 숨기지 말고 `make readme`가 오류로 멈추게 하라. 실패를 숨기면 깨진 실행이 정상처럼 보인다.
- 이미지가 없을 때 'TODO'·'여기에 이미지' 같은 자리 표시 문구를 쓰지 마라. 이유: 공개 문서에는 '촬영 예정'으로 표기한다(사용자 결정).
- LICENSE 문구·파일을 만들지 마라. 이유: 공개 직전 사용자 결정(step 1 체크리스트).
- M5~M7 결과 수치를 README용으로 다시 계산하지 마라. 이유: 확정 결과이며 기존 보고 함수·manifest 값을 그대로 쓴다. 재계산하면 같은 수치가 다르게 나올 수 있다.
- 비커밋 원문(`data/interim`, 실제 규정 조문)을 README에 인용하지 마라. 이유: ADR-002. 실제 규정은 규정명·조 번호까지만 언급한다.
- 로컬 원본이 없는 clone에서 실패하도록 H8 검증을 만들지 마라. 이유: 원본 jsonl은 비커밋이다. 커밋된 manifest 쪽 검증은 필수로, 로컬 원본 검증은 원본이 있을 때만 수행한다(FP-053의 취지: 원본 해시는 실행 시점에 기록하고, 있을 때 대조).
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 6·7이 만드는 `data/interim/m8/` 캐시 쓰기만 허용되며 이 경로도 커밋하지 않는다.
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
