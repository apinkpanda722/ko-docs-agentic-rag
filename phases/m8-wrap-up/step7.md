# Step 7: rerank-dev

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `src/kodocs/retrieval/rerank.py`(step 6)·`tests/retrieval/test_rerank.py` — 상수·래퍼·`reorder`·`choose_max_length`
- `src/kodocs/eval/m5_dev.py`·`m5_inputs.py`·`m5_runner.py`·`m5_metrics.py`·`m5_stats.py` — dev 질문 로딩(`_load_dev_questions`, `EvalQuestion`), 검색 실행 방식(M5 러너가 후보를 만드는 코드), 지표 함수(문서 단위 NDCG@10·Recall@10·구간 겹침 적중률), 정답 집합(asker가 볼 수 있는 rel ≥ 1)
- `src/kodocs/eval/m5_test.py` — 실행 전 검사(`_preflight`, `_check_indexes`), 잠금(`TestLockError`)과 실행 기록, 임베더 생성 방식(`_make_embedder`), 환경 기록(`_environment`), `_BOOTSTRAP_SEED`
- `data/manifest/m5_config.json`·`m5_dev.json`·`m5_eval_inputs.json` — 인덱스 이름·`chunk_count`·`schema_hash`, dev 질문 정의. **주의: `m5_dev.json`의 격자는 고정 청킹(`grid_definition.chunking == "fixed"`)이라 M8 구조 청킹 대조군과 비교하지 않는다**
- `src/kodocs/eval/m7_rawhash.py` — 원본 해시 기록·검증(`record_fingerprint`·`verify_fingerprint`)
- `docs/results/hypotheses.md` 'M8 판정 규칙' 전체(M8-R1~R4, T1, T2)와 `src/kodocs/eval/m8_rules.py`(step 0)
- `src/kodocs/eval/m8_contract.py`(step 4) — 경로 상수
- `src/kodocs/retrieval/embedding.py`(로컬 BGE-M3 임베더, 쿼리 임베딩 캐시)·`search.py`·`opensearch.py`(읽기 전용 호출 방식)
- `.gitignore`(`data/interim/` 무시)·`Makefile`의 `m5-dev`·`m5-freeze` 타깃 형식

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 이 phase(m8-wrap-up) 개요

PRD M8: 공개 가능한 마무리(공개 전환 점검, ADR 정리, Streamlit GUI, README)와 리랭커 실험(H8). 속도 우선이라 필수 항목(step 0~5)을 먼저 끝내 언제든 공개할 수 있는 상태를 만들고, 리랭커(step 6~8)는 그 뒤에 붙인다.

| step | name | 내용 |
|---|---|---|
| 0 | `m8-prereg` | H8 사전 등록(hypotheses.md 'M8 판정 규칙', ADR-031)과 규칙 절 해시 모듈 |
| 1 | `public-audit` | 공개 전환 점검 스캐너(`make m8-audit`·`make m8-audit-gh`)와 점검 문서 |
| 2 | `docs-cleanup` | ADR 상태·색인, M7 정정(패키지명·parse_error 오프라인 분류), 별표 한계를 M8 결과 문서로 |
| 3 | `streamlit-ui` | Streamlit 데모 GUI(질의 화면·결과표 뷰어), answer()만 호출 |
| 4 | `readme` | README 생성기(`make readme`)와 README.md. H8은 결과가 없으면 '미실행' |
| 5 | `ui-streaming` | parse_error 재분류(원장 근거), 최종 답변 실제 스트리밍·도구 호출 실시간 표시, 폭주 재현 실행 코드(`make m8-stream-repro`) |
| 6 | `rerank-core` | 리랭커 상수·CrossEncoder 래퍼·순서 재배열 순수 함수·`make rerank-models` |
| 7 | `rerank-dev` | 후보·점수 캐시, max_length 확정, dev 격자, freeze |
| 8 | `rerank-test` | test 1회 잠금, 대조군 일치 검사, 짝 비교 부트스트랩, 지연, 결과 문서 |

고정 전제(모든 step 공통):
- M5~M7 확정 결과와 고정 설정(m5 `config_hash` `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`, `data/manifest/m6_config.json`, `data/manifest/m7_config.json`)은 소급 변경하지 않는다. M8 실험은 새 구성으로 따로 기록하고 기존 판정과 섞지 않는다.
- M8 결과 문서 파일명 날짜는 고정값 `2026-10-03`이다: `docs/results/2026-10-03-m8-wrap-up.md`(step 2), `2026-10-03-m8-public-readiness.md`(step 1), `2026-10-03-m8-rerank.md`(step 8).
- 이 phase의 step에서는 실제 API 호출, 모델·데이터 다운로드, 패키지 설치, `.env` 열람을 하지 않는다. 사용자가 직접 실행하는 명령은 step 파일이 아니라 사용자가 실행한다.

## 작업

dev에서 리랭커 설정을 고르고 freeze한다. 이 step은 **코드와 테스트만** 만든다. 실제 OpenSearch 조회·모델 로딩·점수 계산은 사용자가 아래 명령으로 실행한다. 모든 테스트는 fake(OpenSearch·임베더·CrossEncoder·토크나이저)로 한다.

모듈은 `src/kodocs/eval/m8_dev.py`다(필요하면 `m8_common.py`에 test step과 공유할 함수를 둔다). CLI 진입점: `--candidates`·`--tokens`·`--score-grid`(기본, `m8-dev`)·`--freeze`.

### 1. 공통: 사전 검사와 후보 캐시
- `preflight_indexes(client, m5_config)`(읽기 전용): 인덱스 이름(`kodocs-m5-composite-structural`)이 `m5_config.json`의 인덱스 표에 있고, 실제 OpenSearch의 `chunk_count`와 `schema_hash`(M5가 계산·기록한 방식과 **같은 함수**, `m5_test._check_indexes` 재사용)가 `m5_config.json`과 같은지 확인한다. 하나라도 다르면 `PreflightError`로 중단한다(사전 등록 M8-X1(2)). 이름·`chunk_count`·`schema_hash` 각각을 따로 테스트한다.
- 설정 검사: `m5_config.json`의 `config_hash`가 `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`와 같다. `m8_rules.rules_sha256()`을 읽어 manifest에 기록한다.
- **후보 캐시**: dev 검색 지표 대상(골드 dev 14 + 실버 dev 43; 문항 목록은 M5 dev와 같은 로더로 얻는다)마다 M5 최종 구성으로 후보 50청크를 가져온다(`prefer_current` 끔, M5 러너의 검색 실행 코드를 재사용하고 새 검색 로직을 만들지 않는다). 후보는 대조군 순서 그대로 `chunk_id`·`doc_id`·`section_key`·`start/end`·**청크 원문**·`base_rank`·대조군 점수와 질문·asker를 담는다. 이 파일은 청크 원문을 포함하므로 **`data/interim/m8/candidates/dev.jsonl`(gitignore 경로)에만** 쓴다.
- 커밋 대상(`data/manifest/m8_candidates.json`)은 `chunk_id`·점수·해시·행 수뿐이다: split별 후보 캐시 파일의 SHA-256·행 수(FP-053), 질문 수, 질문별 chunk_id 목록의 해시(chunk_id 자체는 커밋해도 된다), M5 config_hash. **청크 원문·질문 원문(실제 규정 문항 포함)은 이 manifest에 넣지 않는다.** 테스트: manifest 쓰기 함수의 허용 키 목록에 본문 필드가 없다 / 캐시 기본 경로가 `data/interim/m8/` 아래다 / `git check-ignore`로 그 경로가 무시됨을 확인(저장소 루트에서 실행하는 테스트, git이 없으면 `.gitignore` 패턴 파싱으로 대체) / 커밋 경로(`data/manifest`)에 원문 필드가 쓰이면 실패하는 가드.
- 후보 캐시 파일이 이미 있으면 새로 만들지 않고 해시를 검증한다: manifest의 SHA-256·행 수와 같으면 건너뛰고, 다르면 거부한다(덮어쓰기는 `--force` 없이 하지 않는다).

### 2. `make m8-tokens` — max_length 확정 (모델 점수 계산 없음)
- 입력은 dev 후보 캐시와 리랭커 **토크나이저**뿐이다(모델 가중치는 로드하지 않는다). 토크나이저는 `resolve_local_reranker_snapshot()`(step 6)의 로컬 경로에서 오프라인으로 로드한다(BGE-M3 토크나이저와 같다고 가정하지 않는다 — 리랭커 자체 것을 쓴다).
- dev 후보 쌍 전체(질문 + 청크 + 특수 토큰, `truncation=False`)의 토큰 길이를 `measure_pair_lengths`로 잰다. 규칙 M8-R4에 따라 `choose_max_length(lengths, cap=1024)`를 적용한다.
- `data/manifest/m8_max_length.json`(덮어쓰기 거부, `_write_json_exclusive` 방식)에 쓴다: `max_length`, `rule`(문구 요약), `p50`·`p90`·`p99`·`max`, `n_pairs`, `capped`, dev 잘린 쌍 수·비율, 토크나이저 revision·경로 해시, 후보 캐시 SHA-256, 규칙 절 SHA-256, 측정 시각, 코드 커밋. 분포에는 청크·질문 텍스트가 없다.
- 이 manifest가 **없으면 `m8-dev` 점수 계산은 거부한다**(`PreflightError`: "먼저 `make m8-tokens` 실행"). 테스트로 강제한다.

### 3. `make m8-dev` — 점수 캐시와 dev 격자
- 필수 선행: `m8_max_length.json`(위), `m8_candidates.json`(+ 캐시 해시 일치), 인덱스 사전 검사(1번).
- **점수 캐시**: dev 후보 50청크를 쌍마다 **한 번만** 채점해 `data/interim/m8/scores/dev.jsonl`에 쓴다. 캐시 키는 채점 요청 전체의 지문이다: `(question_id, chunk_id, 질문 텍스트 SHA-256, 청크 텍스트 SHA-256, max_length, 모델 revision)`. 키가 같으면 재사용하고 하나라도 다르면 다시 채점한다(FP-054·FP-033: 키가 입력의 일부만 담으면 다른 입력의 점수를 복사한다). 청크 텍스트가 바뀐 후보가 재채점되는 테스트를 둔다.
- 모델 로딩은 `init_s`로 따로 재고, 점수 계산 시간·장치·batch_size·`max_length`·MPS 메모리 스냅샷(`mps_memory_snapshot`, 기록 전용)을 manifest에 기록한다(FP-036·FP-044). 임베더(BGE-M3)와 리랭커를 함께 올린 상태에서 메모리를 기록한다.
- **격자**: M8-T1의 6개(`depth ∈ {20,30,50}` × `fusion ∈ {alone, rrf}`)를 점수 캐시에서 오프라인으로 계산한다. 지표는 M5와 같은 함수(문서 단위 NDCG@10, 정답 집합 = asker가 볼 수 있는 rel ≥ 1)를 재사용하고 골드 dev 14·실버 dev 43 각각 평균을 낸다. 선택 점수 = (골드 dev 평균 NDCG@10 + 실버 dev 평균 NDCG@10) / 2, 동점이면 `alone`, 그다음 작은 depth(`select_setting`).
- **dev 대조군은 기록만 한다(M5와 비교하지 않는다)**: 같은 후보에서 재정렬 없이 계산한 **대조군 골드 dev·실버 dev 평균 NDCG@10**(과 Recall@10)을 `m8_dev.json`에 기록한다. `data/manifest/m5_dev.json`과 대조하지 않는다. 이유: M5 dev 격자는 **고정 청킹** 인덱스에서 돌았다(`m5_dev.json`의 `grid_definition.chunking == "fixed"`). M8 대조군은 구조 청킹(`composite-structural`)이라 값이 일치할 수 없고, 비교하면 `make m8-dev`가 항상 중단된다. M5에는 구조 청킹 dev 격자 기록이 없으므로 이 사실을 `m8_dev.json`의 `baseline_note` 필드(문구: "M5 dev 격자는 고정 청킹 기준이라 구조 청킹 대조군의 dev 값은 M5에 기록이 없다. M8에서 처음 기록한다.")에 남기고 step 8의 결과 문서가 한 줄로 옮겨 적는다. 대조군 검증은 step 8의 test 검사(M8-X1(3): M5 test 문항별 값과 일치)로만 한다. 사전 등록 문구(M8-X1)는 test 대조만 규정하므로 문구를 바꾸지 않는다.
- `data/manifest/m8_dev.json`(덮어쓰기 거부, 재실행은 파일을 사용자가 지운 뒤): 6개 설정의 골드·실버 dev NDCG@10·선택 점수, 선택 행, 대조군(재정렬 없음) 골드·실버 dev 평균 NDCG@10·Recall@10(기록만)과 `baseline_note`, 점수 캐시 SHA-256·행 수(FP-053), `max_length`, 모델 revision, `init_s`, 장치·batch_size, MPS 메모리, 후보 manifest 해시, 규칙 절 해시, 코드 커밋(`git rev-parse HEAD` — 없으면 `null`). 청크·질문 원문 없음.

### 4. `make m8-freeze`
- `data/manifest/m8_dev.json`을 읽어 `data/manifest/m8_config.json`(**덮어쓰기 거부**)에 선택 설정을 고정한다: 선택 `depth`·`fusion`, `max_length`, 모델 id·revision, `rerank_rank_constant=60`, m5 `config_hash`, `m8_dev.json` SHA-256, `m8_max_length.json` SHA-256, 점수 캐시·후보 캐시 지문, 규칙 절 SHA-256(`m8_rules.rules_sha256()`), 장치·batch_size, `frozen_at`, 코드 커밋, `config_hash`(**freeze 내부 `config_hash` = 나머지 필드의 정규 JSON SHA-256, `m5_dev._config_hash` 방식 — M5~M7과 같은 뜻이며 `m8_config.json` 파일 바이트의 SHA-256과 다르다.** 파일 SHA-256은 step 8의 결과 계약이 `config_file_sha256`로 따로 기록·검증한다). `verify_config(path)`는 파일을 다시 읽어 해시와 현재 규칙 절 해시를 대조한다. 규칙 절 해시가 다르면 거부한다(사전 등록 후 문구 변경 감지).
- freeze 이후 dev를 다시 실행해 설정을 바꾸는 경로는 만들지 않는다.

### 5. Makefile·AGENTS.md
- `.PHONY`에 `m8-candidates m8-tokens m8-dev m8-freeze`를 추가한다. 타깃은 M5 형식을 따른다: `m8-candidates`·`m8-tokens`·`m8-dev`는 `uv run --group embed python -m kodocs.eval.m8_dev --candidates|--tokens|(기본)`, `m8-freeze`는 `uv run python -m kodocs.eval.m8_dev --freeze`.
- `AGENTS.md` `## 명령어`에 네 줄을 추가한다(각각: 후보 캐시 — make up 필요·OpenSearch 읽기 전용·리랭커 점수 계산 없음, max_length 측정 — 모델 가중치 없이 토크나이저만, dev 격자 — 리랭커 로딩·MPS, freeze — 덮어쓰기 거부). 모두 "사용자 실행".

### 6. 테스트 (`tests/eval/test_m8_dev.py`)
- 인덱스 사전 검사 세 가지 불일치(이름·chunk_count·schema_hash)가 각각 중단 / config_hash 불일치 중단.
- `m8_max_length.json` 없이 `m8-dev` 거부, 있어도 후보 manifest 해시 불일치 거부, max_length manifest 덮어쓰기 거부.
- 격자가 정확히 6개이고 선택 규칙(동점 `alone` → 작은 depth)이 동작한다 / dev 대조군 평균이 `m8_dev.json`에 **기록됨**(`baseline_note` 포함)이고 `m5_dev.json`과 비교하지 않는다(`m5_dev.json`이 없거나 값이 달라도 중단하지 않음) / 점수 캐시 키가 청크 텍스트·질문·max_length·revision 변화에 반응한다.
- freeze: 덮어쓰기 거부, 규칙 절 해시 불일치 거부, 해시 재계산이 일치.
- 원본 지문(후보·점수 캐시 SHA-256·행 수)이 manifest에 기록되고 이후 검증이 현재 파일과 대조한다(FP-053).
- 원문이 커밋 경로로 나가지 않음(1번 테스트).
- 통합 테스트가 필요하면 OpenSearch **읽기 전용**(`_count`·`_search`)만 쓰고 실측 인덱스를 만들거나 지우지 않는다(FP-046). 새 통합 테스트가 필요 없으면 만들지 않는다.

## Acceptance Criteria

```bash
make verify   # ruff + pytest 통과(모든 외부 의존은 fake)
make -n m8-candidates m8-tokens m8-dev m8-freeze   # 타깃이 선언돼 있는지만 확인(실행하지 않는다)
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

- `make m8-candidates`·`m8-tokens`·`m8-dev`·`m8-freeze`를 실행하지 마라(OpenSearch 조회·모델 로딩 포함). 이유: 사용자 실행이다. 이 step의 테스트는 fake만 쓴다(FP-040).
- 후보·점수 캐시의 **청크 원문과 질문 원문**을 `data/manifest/`나 그 밖의 커밋 경로·로그·예외 메시지에 쓰지 마라. 캐시는 `data/interim/m8/`(gitignore)에만 둔다. 이유: 원문·정제 산출물은 비커밋이다(ADR-002, 실제 규정 조문이 청크에 들어 있다). 커밋 대상은 `chunk_id`·점수·해시·행 수뿐이다.
- dev 격자 밖 설정(다른 depth, 다른 fusion, 다른 max_length, 접두어·재작성)을 시험하거나 test 문항을 보고 설정을 고르지 마라. 이유: 사전 등록 M8-T1·T2. 튜닝은 골드 dev 14·실버 dev 43으로만 한다.
- `m8_max_length.json` 없이 점수를 계산하거나 max_length를 코드에 하드코딩하지 마라. 이유: 값은 dev 후보 쌍의 P99 규칙으로 점수 계산 **전에** 확정한다(M8-R4).
- 점수 캐시 키를 질문 ID·청크 ID만으로 만들지 마라. 이유: FP-054·FP-033(요청 일부만 키로 쓰면 다른 입력의 점수를 복사한다). 텍스트 해시·max_length·revision을 포함한다.
- 실측 인덱스를 만들거나 지우지 마라. OpenSearch는 읽기 전용(`_count`·`_search`·`_settings`·`_mapping` GET)만 쓴다. 이유: FP-046.
- freeze·dev manifest를 덮어쓰거나 freeze 이후 설정을 바꾸는 경로를 만들지 마라. 이유: 사전 등록 잠금(M8-T2). 필요하면 사용자가 파일을 지우고 이력에 사유를 남긴다.
- dev 대조군을 `m5_dev.json`의 값과 비교해 중단하게 만들지 마라. 이유: M5 dev 격자는 고정 청킹 기준이라 구조 청킹 대조군과 일치할 수 없다(항상 중단된다). dev 대조군은 기록만 하고, 후보가 M5와 같은지는 인덱스 사전 검사(1번)와 step 8의 test 대조(M8-X1(3))로 확인한다.
- 모델 로딩 시간을 문항별 점수 시간에 합산하지 마라. 이유: FP-036. `init_s`를 따로 기록한다.
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 5·7·8이 만드는 코드가 **사용자 실행 시** 쓰는 `data/interim/m8/`(재현 원시 출력·후보·점수 캐시)만 허용되며 이 경로도 커밋하지 않는다. 에이전트가 step 안에서 이 경로에 쓰지는 않는다(실행은 사용자).
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
