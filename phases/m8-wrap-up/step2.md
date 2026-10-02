# Step 2: docs-cleanup

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `docs/ADR.md` 전체 — ADR-001~031의 제목 줄(`### ADR-NNN: ...`), 이미 있는 '상태'·'변경'·'보완' 표기(ADR-004 변경 주석, ADR-020 폐기, ADR-015·025의 '보완(M5)' 등), 본문 속 `ADR-NNN` 상호 참조
- `tests/test_threat_model_doc.py` — 문서 일관성 테스트 방식
- `docs/results/2026-10-02-m7-answer-eval.md` — '실행 정보'·'비용'·'지연 비캐시 보정치' 생성 영역과 '한계(사용자 보존 영역)'의 '지연 한계 3(환경 기록)' 항목, 'M8 개선 후보' 절
- `src/kodocs/eval/m7_report.py` — 결과 문서 생성기 구조(생성 영역 마커, 보존 영역 유지 방식)
- `src/kodocs/eval/m7_latency.py`(`PACKAGE_NAMES`, `collect_environment`)와 `data/manifest/m7_latency.json`의 environment 필드 — **기록된 패키지명 값**
- `pyproject.toml`·`uv.lock`(읽기만) — 실제 설치·사용 패키지 이름과 버전
- `src/kodocs/retrieval/embedding.py`·`opensearch.py` — 실제 사용 라이브러리 import 위치(`sentence_transformers`·`httpx` 등)
- `data/manifest/m7_failures.json`(`measurement_limitations`)·`src/kodocs/eval/m7_failures.py` — 별표 맥락 유실 감사(`annex_context_audit`)를 만든 코드
- `src/kodocs/chunking/corpus.py`의 `_composite_text_and_sections`(읽기만, 수정 금지)
- `data/golden/queries/gold.jsonl`·`silver.jsonl`의 `qrels[].annex_id` 필드
- `logs/traces/2026-10-02.jsonl`과 `data/interim/m7/latency/runs.jsonl`의 **실제 한 줄**(비커밋 로컬 파일; 필드 이름·`refuse_reason`·`parse_error`가 기록된 위치와 오류 메시지 필드가 있는지 확인용)

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

M8 문서 정리 세 가지를 한 step에서 한다: (A) ADR 상태·상호 참조·색인, (B) M7 잔여 정정(패키지명, 스트리밍 parse_error 오프라인 분류), (C) 별표 조립 결함 한계 기록. 정정 내용은 **M8 결과 문서 `docs/results/2026-10-03-m8-wrap-up.md`**에 쓴다.

### A. ADR 상태·색인 (`docs/ADR.md`, `docs/ADR-INDEX.md`)
- **본문을 다시 쓰지 않는다.** `docs/ADR.md`에는 각 ADR 제목 줄 바로 아래에 상태 한 줄만 **추가**한다: `**상태**: 유효` / `**상태**: 유효(일부 변경: ADR-0NN)` / `**상태**: 폐기` / `**상태**: 대체됨(→ ADR-0NN)`. 허용 값 집합과 형식은 상수로 정의한다. ADR-020은 이미 `**상태**: 폐기 — ...` 줄이 있으므로 중복 추가하지 않고 형식만 검사한다.
- 상태는 문서에 이미 있는 변경·보완 표기에서 정한다(예: ADR-004는 '변경: 채점 모델 등급은 ADR-030에서 변경' → `유효(일부 변경: ADR-030)`). 문서에 근거가 없는 ADR은 `유효`로 둔다. 근거가 모호해 판단이 필요한 ADR은 목록으로 최종 보고 summary에 적는다(사용자가 검토).
- `docs/ADR-INDEX.md`는 **생성 파일**이다: ADR 번호·제목·상태·본문 속 `ADR-NNN` 참조(나가는 참조·들어오는 참조)·`ADR.md` 앵커 링크를 표로 만든다. 생성 코드는 `src/kodocs/eval/m8_adr_index.py`, 타깃은 `make adr-index`. 생성 파일 맨 위에 "이 파일은 `make adr-index`가 생성한다. 손으로 고치지 않는다."를 쓴다.
- 테스트(`tests/eval/test_m8_adr_index.py`): 모든 ADR 제목에 상태 줄이 있고 허용 값이다 / 색인에 모든 ADR 번호가 순서대로 있다 / 본문의 `ADR-NNN` 참조가 모두 존재하는 ADR을 가리킨다 / '대체됨(→ADR-N)'·'변경: ADR-N' 상태가 가리키는 ADR이 존재한다 / 커밋된 `ADR-INDEX.md`가 생성 결과와 같다(최신 여부). 테스트는 git 상태에 의존하지 않는다. '상태 줄 추가 외에 본문이 바뀌지 않았는지'는 테스트가 아니라 작업 후 `git diff --stat docs/ADR.md`가 추가 줄만 있는지(삭제 0줄) 직접 확인해 최종 보고에 적는다.

### B. M7 잔여 정정 → `docs/results/2026-10-03-m8-wrap-up.md`
이 문서는 생성기 `src/kodocs/eval/m8_wrap_report.py`(`make m8-wrap-report`, 네트워크 없음)가 `<!-- BEGIN generated -->` 영역을 쓰고, '사용자 보존 영역'은 건드리지 않는다(`m7_report.py` 방식). 이 영역은 step 7 이후 사용자가 직접 보충할 수 있다.

1. **패키지명 정정**(M7 지연 한계 3). M7 환경 기록에 `FlagEmbedding`·`opensearch-py`가 미설치로 적혀 있으나 이는 라이브러리 선택과 다른 수집 목록 오류이고 실제 사용 라이브러리는 sentence-transformers(임베딩)와 httpx(OpenSearch 직접 HTTP 호출)다. 생성기가 `data/manifest/m7_latency.json`의 기록값, `uv.lock`의 실제 버전, `src/`의 import 위치를 읽어 표(기록값 / 실제 사용 / 근거 파일·줄)로 만든다. 값을 손으로 적지 않는다. **M7 manifest(`m7_latency.json` 등 해시 대상)는 수정하지 않는다**(FP-053). 임베딩 revision은 `data/manifest/m5_config.json`에서 읽는다.
2. **M7 결과 문서 한 줄 참조**: `docs/results/2026-10-02-m7-answer-eval.md`의 '한계(사용자 보존 영역)' 중 '지연 한계 3(환경 기록)' 항목 **끝에 한 문장만** 추가한다: ` M8 정정 상세: [2026-10-03-m8-wrap-up.md](2026-10-03-m8-wrap-up.md).` 생성 영역(`BEGIN/END generated`)과 다른 항목은 바꾸지 않는다. `make m7-report`는 실행하지 않는다(생성 영역이 다시 쓰인다). `git diff docs/results/2026-10-02-m7-answer-eval.md`가 정확히 이 한 줄 변경인지 확인해 보고한다.
3. **스트리밍 parse_error 4건 오프라인 분류**(M7 지연 한계 2). 재호출 없음, 로컬 기록만 읽는다.
   - 모듈 `src/kodocs/eval/m8_parse_error.py`, 타깃 `make m8-parse-error`. 입력은 M7 지연 실행 원본(`data/interim/m7/latency/runs.jsonl`)과 필요 시 `logs/traces/*.jsonl`이다. 먼저 **실제 한 줄을 읽어** `parse_error`가 기록된 필드·오류 메시지·원시 출력 길이 같은 단서가 무엇인지 확인한다(FP-011: 필드 이름 추측 금지).
   - parse_error 행(시스템·문항·모델)을 결정론으로 분류한다. 분류 범주는 기록에 근거가 있는 것만 만든다(예: 빈 출력, JSON 잘림(max_output_tokens 도달), 스키마 위반, 스트림 중단). 판정에 필요한 기록이 없으면 `indeterminate`와 사유로 센다. **추측으로 원인을 확정하지 않는다.** 4건이라는 건수는 M7 결과 문서의 값이므로 코드가 센 값과 대조하고 다르면 둘 다 보고한다.
   - 비스트리밍 M6 실행에서 같은 문항·시스템의 parse_error 여부는 M6 산출물이 있을 때만 참고 정보로 병기한다(분류 입력이 아니다).
   - 출력: `data/manifest/m8_parse_error.json`(문항 ID·시스템·분류·사유·건수, **답변 본문·원시 출력 텍스트 없음**). 이 결과를 wrap-up 문서 생성 영역과 README 한계가 읽는다.
4. **원본 해시 대조**: 위 분석 전에 `data/manifest/m7_latency.json`에 기록된 원본 `runs.jsonl`의 SHA-256·행 수를 현재 파일과 대조한다(`m7_rawhash.verify_fingerprint` 재사용). 어긋나면 분석을 거부한다(FP-053).

### C. 별표 조립 결함 한계(수정하지 않음)
- 수정·재색인·코퍼스 v2는 하지 않는다. 한계로만 기록한다(사전 등록 N1).
- wrap-up 문서 생성 영역에 코드가 센 값을 쓴다: 영향 별표 수(composite가 pypdf의 70% 미만인 별표 / 비교 별표 수), 단위 표기가 유실된 별표 수와 규정 수(`m7_failures.json`의 `annex_context_audit` 값을 읽거나 같은 감사 함수를 재사용).
- **영향 평가 문항 수는 별표 단위로 센다**(규정 단위로 세면 같은 규정의 다른 별표 문항까지 들어간다). 골드·실버 질문의 `qrels[].annex_id`가 있는 항목의 `(doc_id, annex_id)`가 아래 두 집합에 속하는 문항을 **split별(골드·실버 × dev·test)로 각각** 센다: (가) 단위 표기가 유실된 별표 14개의 집합, (나) composite가 pypdf의 70% 미만인 별표 95개의 집합. 두 집합은 겹칠 수 있고(가가 나의 부분집합일 수 있다) 각각 따로 센다. 세는 단위는 중복 없는 **문항 수**다(FP-043). 현재 규정 단위로는 골드 test 1개(g-real-007)가 걸리지만 이 값은 별표 단위 기준이 아니므로 기대값으로 쓰지 말고 코드가 센 값을 쓴다.
- 두 별표 집합의 식별자 `(doc_id, annex_id)`는 `m7_failures.py`의 별표 감사(composite vs pypdf 별표 섹션 비교)에서 얻는다. 그 함수가 식별자를 돌려주지 않으면 같은 함수를 import해 읽기 전용으로 식별자를 얻는 얇은 어댑터를 만든다(`m7_failures.py`·`corpus.py`는 수정하지 않는다). 식별자를 얻을 수 없으면 규정 목록 상수로 대체하지 말고 `blocked`로 보고한다. 얻은 집합의 크기가 14·95와 같은지 테스트로 확인한다(다르면 둘 다 보고하고 중단).
- 문장으로 쓴다: "M5~M7 수치는 이 유실이 있는 채로 측정되었다. 수정·재색인은 하지 않았다(별표 단위 영향 문항이 split별로 N개라 v2 재측정으로 얻을 정보가 없다)."

### D. 생성기·Makefile·AGENTS.md
- `.PHONY`에 `adr-index m8-wrap-report m8-parse-error`를 추가하고 타깃을 만든다(모두 `uv run python -m ...`, 네트워크 없음).
- `AGENTS.md` `## 명령어`에 세 명령을 한 줄씩 추가한다.
- 테스트: wrap-up 생성기가 보존 영역을 유지하는지 / 패키지명 표가 fixture manifest로 생성되는지(기록값 → 실제값 매핑) / parse_error 분류가 fixture 행에서 범주·`indeterminate`·건수 불일치 보고를 내는지 / 원본 해시 불일치 시 분석 거부 / 출력 manifest에 답변 본문 필드가 없는지.
- 마지막에 `make adr-index`, `make m8-parse-error`, `make m8-wrap-report`를 순서대로 실행해 산출물을 만든다(모두 오프라인).

## Acceptance Criteria

```bash
make verify   # ruff + pytest 통과
make adr-index && make m8-parse-error && make m8-wrap-report   # 오프라인 산출물 생성
git diff --stat docs/ADR.md docs/results/2026-10-02-m7-answer-eval.md   # ADR.md는 추가 줄만, M7 문서는 한 줄만 변경됐는지 확인
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

- `docs/ADR.md` 본문(상태 줄 추가 외)을 다시 쓰거나 삭제하지 마라. 이유: ADR은 결정 시점 기록이다. 정리는 상태와 색인에 한정한다.
- M7 manifest(`m7_latency.json`·`m7_config.json` 등)를 수정하지 마라. M7 결과 문서는 '지연 한계 3' 항목 끝 한 문장만 추가하고 생성 영역은 건드리지 마라. 이유: 해시 대상이 깨진다(FP-053). 정정 상세는 M8 결과 문서에 쓴다.
- `make m7-report`를 실행하지 마라. 이유: M7 결과 문서의 생성 영역이 다시 쓰이고 커밋 해시가 바뀐다.
- parse_error 원인을 기록 없이 추측해 확정하지 마라. 로그·트레이스를 근거로 못 가르면 `indeterminate`다. 이유: M7-F3와 같은 원칙(재호출·재검색으로 채우지 않는다)과 FP-011. API를 다시 호출하지 마라.
- 별표 조립(`corpus.py`)을 고치거나 재색인하지 마라. 이유: 사용자 결정(수정·검증 없는 코드 수정 모두 제외), 영향 문항이 골드 test 1개다.
- 패키지명 정정 표를 손으로 쓰지 마라. 이유: 값은 manifest·`uv.lock`·소스에서 생성한다(손으로 옮기면 어긋난다).
- parse_error 분석 출력에 답변 본문·원시 모델 출력을 넣지 마라. 이유: 비커밋 원문 보호(실제 규정 근거가 답변에 섞일 수 있다, ADR-002).
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 6·7이 만드는 `data/interim/m8/` 캐시 쓰기만 허용되며 이 경로도 커밋하지 않는다.
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
