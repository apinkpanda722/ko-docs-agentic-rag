# Step 8: rerank-test

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `src/kodocs/eval/m8_dev.py`·`m8_common.py`(step 7)·`m8_contract.py`(step 4)·`retrieval/rerank.py`(step 6) — 재사용할 후보 생성·점수 캐시·재배열·검사 함수와 **결과 계약**(경로·필수 키·`validate_result`)
- `src/kodocs/eval/m5_test.py` — 실행 잠금(`_preflight`, `TestLockError`, `RERUN_REASON` 규칙, 실행 기록 append, 결과 쓰기 `_write_json_exclusive`·`_write_raw_exclusive`, 실패 기록)과 `analyze_results`·부트스트랩 호출 방식
- `src/kodocs/eval/m5_stats.py`(`paired_bootstrap`, `verdict`)·`m5_metrics.py`·`m5_analysis.py` — 판정·지표·출처별 분리
- `data/manifest/m5_test_results/run-01.json` — **집계만** 있고 문항별 NDCG@10은 없다(`analysis.full_grid.rows`·`analysis.ladder.stages`의 최종 구성 행 구조를 실제 파일로 확인한다, FP-011)
- `data/interim/m5/run-01/results.json`(비커밋 로컬) — 문항별 값의 출처: `runs[]` 각 항목이 `config`·`question_set`·`split`·`questions[].qid`·`ndcg10`을 가진다(실제 파일을 읽어 키를 확인한다)
- `src/kodocs/eval/m7_latency.py`(`nearest_rank`, `_statistics`, `_p95_ci`, 지연 질문 목록·예열 방식, 잠금·`RERUN_REASON`, 원본 지문)와 `data/manifest/m7_latency.json`
- `src/kodocs/eval/m7_failures.py`·`data/manifest/m7_failures.json` — d1 문항 ID(골드 g-syn-007·g-syn-012, 실버 s-syn-003·014·033·046·050·087·116)와 후보 기록이 없는 indeterminate 사유
- `src/kodocs/eval/m7_report.py`·`m5_report.py` — 결과 문서 생성기 구조(생성 영역·사용자 보존 영역)와 `templates/` 형식
- `docs/results/hypotheses.md` 'M8 판정 규칙' 전체와 `m8_rules.py`
- `docs/results/2026-09-30-m5-retrieval.md` — 사다리 표(리랭커 행을 M8 문서에서 이어 붙일 형태)

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

test를 **한 번** 실행하는 코드, 지연 측정 코드, 결과 문서 생성기를 만든다. 실제 실행은 사용자가 한다. 모든 테스트는 fake로 한다.

모듈: `src/kodocs/eval/m8_test.py`(`make m8-test`), `src/kodocs/eval/m8_latency.py`(`make m8-latency`), `src/kodocs/eval/m8_report.py`(`make m8-report`), 템플릿 `src/kodocs/eval/templates/m8_rerank.md`.

### 1. `make m8-test` — M8-X1
순서가 중요하다. 앞 검사가 하나라도 실패하면 **어떤 출력 파일도 쓰지 않고** 중단한다.
1. **freeze 확인**: `m8_dev.verify_config(m8_config.json)`(규칙 절 해시 일치 포함). freeze가 없으면 거부.
2. **잠금**: `data/manifest/m8_test_runs.jsonl`에 기존 기록이 있으면 `RERUN_REASON`이 없을 때 `TestLockError`(M5 `m5_test`의 방식 그대로: 사유·날짜·이전 결과 해시를 실행 기록에 누적, 결과 문서에는 첫 실행과 재실행을 모두 보고). `RERUN_REASON`은 환경 변수 → CLI `--rerun-reason`(Makefile이 변환)으로 전달한다.
3. **인덱스 검사(B)**: 인덱스 이름·`chunk_count`·`schema_hash`가 `m5_config.json`과 같은지 먼저 검사한다(`preflight_indexes` 재사용). 다르면 중단.
4. **후보 생성**: 골드 test 검색 지표 대상 21 + 실버 test 65(M5 test와 같은 문항 목록)에 대해 M5 최종 구성으로 후보 50청크를 가져와 `data/interim/m8/candidates/test.jsonl`(비커밋)에 쓴다. 청크 원문은 이 경로에만 있다.
5. **대조군 일치 검사**(점수 계산 전, 리랭커 로드 전): 비교 원본은 커밋 manifest가 아니라 **비커밋 로컬 파일 `data/interim/m5/run-01/results.json`**이다(커밋된 `m5_test_results/run-01.json`에는 문항별 NDCG가 없고 집계뿐이다). 순서:
   - a. 이 파일의 `runs[]`에서 `config`가 M5 최종 구성과 **키 전부** 정확히 같고 `split == "test"`이며 `question_set`이 `gold`인 run, `silver`인 run을 각각 하나씩 고른다. 비교 키: `condition=composite`, `chunking=structural`, `analyzer=nori`, `method=hybrid_weighted`, `bm25_weight=0.7`, `candidate_chunks=50`, `merged_analyzers=false`(키 집합이 다르면 불일치). 정확히 하나가 아니면(0개·2개 이상) 중단한다. 설정만으로는 골드 dev 6문항 run(파서 비교용)까지 걸리므로 `question_set`·`split` 필터가 필요하다(실제 파일 확인: 골드 test 21·실버 test 65문항 run이 각각 하나). 고른 run의 `questions` 수가 21·65와 같아야 한다.
   - b. **로컬 파일과 커밋 기록 묶기**: 이 로컬 파일은 M5 manifest에 해시 기록이 없다. 그래서 고른 run의 평균 NDCG@10이 커밋된 `m5_test_results/run-01.json`의 같은 최종 구성 집계값(`analysis.full_grid` 또는 `analysis.ladder`의 같은 설정 행; 골드·실버 각각)과 1e-9 안에서 같은지 **먼저** 확인한다. 다르면 중단한다.
   - c. 그다음 **문항별 대조**: 후보로 다시 계산한 문항별 문서 단위 NDCG@10(재정렬 없음)을 고른 run의 `questions[].qid`·`ndcg10`과 1e-9 안에서 대조한다. 하나라도 어긋나면 어긋난 문항 ID와 값을 보고하고 중단한다.
   - d. 이 로컬 파일의 SHA-256·행 수(고른 run들의 문항 수 포함)를 m8 test 실행 기록(`RUNS_PATH`)에 `m5_local_results: {sha256, rows, note: "M5 기록 없음, M8에서 처음 기록"}`로 남긴다. 결과 계약(`m8_contract`)의 필수 키는 바꾸지 않는다(이 필드는 실행 기록에만 둔다).
   - e. 로컬 파일이 없으면 `blocked`가 아니라 **명확한 오류**(`PreflightError`: "M5 문항별 결과 파일 없음: data/interim/m5/run-01/results.json")로 점수 계산 전에 중단한다. 이 파일을 수정·생성·재생성하지 않는다(`make m5-test`를 다시 실행하지 않는다).
6. **점수·재정렬**: 리랭커를 로드(`init_s` 별도 기록)해 test 후보 50청크를 쌍마다 한 번 채점해 `data/interim/m8/scores/test.jsonl`에 쓰고(키는 step 7의 캐시 키와 같다), 확정 `depth`·`fusion`·`max_length`로 재정렬한다.
7. **분석**: 
   - **H8**: 골드 21문항의 문항별 문서 단위 NDCG@10 차이(재정렬 − 대조군)를 `m5_stats.paired_bootstrap`(10,000회, 시드 20260930 — `m5_test._BOOTSTRAP_SEED`와 같은 상수를 import한다)으로 분석하고 판정 문구는 사전 등록 세 가지(`채택` / `기각(반대 방향)` / `판정 보류(차이 입증 못 함)`)만 쓴다. 판정 보류 문구 뒤에 사전 등록 문장("그 경우에도 결과를 그대로 보고한다")을 붙이지 말고 생성 문서 템플릿이 쓴다.
   - **보조(M8-H8b, 판정 아님)**: Recall@10, 구간 겹침 적중률(M5 함수), 실제/합성 분리, 유형별 기술 통계, 실버 65 방향 확인(같은 방법), **순위가 나빠진 문항 수**(문항별 NDCG 차이 < 0, 골드·실버), 후보 50 문서 단위 Recall@50, d1 문항 표(`m7_failures.json`의 d1 문항 ID에서 test에 속한 검색 지표 대상 문항만: 보이는 rel=2 문서가 후보 50에 있는지, 재정렬 전후 순위; 검색 지표 대상이 아니거나 후보 기록이 없으면 '대상 외'), test 후보 쌍의 **잘린 쌍 비율**(확정 max_length 기준; 보고만, 규칙 변경 없음).
   - `selected`·`max_length`·`device`·`init_s`·`batch_size`·MPS 메모리·모델 revision을 결과에 기록한다. **해시는 두 값을 모두 기록한다**: `config_hash` = `verify_config`로 검증한 freeze 내부 값(`m8_config.json`의 `config_hash` 필드, 나머지 필드의 정규 JSON SHA-256), `config_file_sha256` = 실행 시점의 `m8_config.json` 파일 바이트 SHA-256. 두 값은 의미가 다르다(5번 계약 수정 참조).
8. **결과 쓰기**: 계약(`m8_contract.validate_result`)을 만족하는 payload를 `RESULT_PATH`(`data/manifest/m8_test_results/run-01.json`, 덮어쓰기 거부, 재실행은 `run-02.json` 등 M5 방식)에 쓴다. 원본 실행 jsonl(`data/interim/m8/test/run-01/runs.jsonl`: 문항별 질문 ID·대조군/재정렬 순위·NDCG 등, 청크 원문 없음)의 SHA-256·행 수를 **실행 시점(완료·실패·정지 시)에** `RUNS_PATH`(`data/manifest/m8_test_runs.jsonl`)에 `result_sha256`과 함께 append한다(FP-053). 실패하면 failed 기록을 남기고 부분 결과를 결과 경로에 쓰지 않는다. 결과 JSON에는 청크·질문 원문이 없다.
- 테스트: 잠금(재실행 거부, 사유 있으면 허용·누적) / 인덱스 불일치 중단과 출력 없음 / 대조군 검사 각각 중단(**설정 매칭 0개, 설정 매칭 2개 이상(같은 question_set·split 중복), 집계값 불일치(b), 문항 불일치(c, 어긋난 문항 ID 보고), 로컬 파일 없음(e)**)하고 모두 리랭커가 로드되기 전에 중단·출력 없음 / 실행 기록에 `m5_local_results` 지문(note 포함)이 남음 / 판정 문구 세 가지(경계 포함) / 순위 악화 문항 수 정의(차이 < 0, 0은 세지 않음) / 결과 payload가 `validate_result`를 통과 / 실행 기록의 원본 해시·행 수가 실제 원본과 일치 / 결과에 원문 없음 / 실패 시 결과 파일 없음.

### 2. `make m8-latency` — M8-P1·P2 (LLM 호출 없음)
- 대상은 M7 지연과 같은 113문항이다(질문 목록·예열 문항은 `m7_latency.py`·`m7_latency.json`에서 읽는다). 동시성 1(`require_serial` 재사용), 시스템 구분 없이 **검색 + 재정렬만** 실행한다: 문항마다 `embed_s`·`search_s`(M5 최종 구성 검색)·`rerank_s`(확정 설정으로 후보 50 채점 + 재배열; 모델 로딩은 `init_s`로 분리)를 `time.perf_counter`로 잰다. 예열 3건(dev, 대상 외)은 제외한다. 항목 하나의 실패는 실패 기록으로 세고 계속한다(FP-030). 실패도 지연 통계에 포함할지는 `m7_latency.py`의 규칙을 따른다(가드레일·실패도 지연으로 포함).
- 통계: `rerank_s`의 P50·P95(`m7_latency.nearest_rank` 재사용)·최대·평균, P95 부트스트랩 95% CI(병기, 시드는 `m7_latency.py`의 지연 부트스트랩 시드를 재사용). **보수적 합산**: M7의 단순 RAG 전체 P95(`data/manifest/m7_latency.json`에서 읽는다 — 손으로 쓰지 않는다) + `rerank_s` P95 = `conservative_sum_s`, 5.0초 목표 대비 `margin_to_5s`(초). 결과 JSON·문서에 "두 P95를 단순히 더한 보수적 근사이며 분포를 합성한 값이 아니다. M7 값은 임베딩 캐시 적중 상태에서 쟀다"를 문구로 포함한다. 판정 문구를 만들지 않는다(M8-P2: 목표 미등록).
- 환경 기록: 장치·batch_size·`max_length`·두 모델(BGE-M3 임베더 + 리랭커)을 함께 올렸을 때의 MPS 메모리 스냅샷(기록 전용), 임베딩 캐시 사용 여부, 패키지 버전.
- 잠금·재실행은 M7 지연과 같다(`RERUN_REASON`). 원본(`data/interim/m8/latency/runs.jsonl`) SHA-256·행 수를 실행 시점에 `data/manifest/m8_latency.json`에 기록한다(FP-053). `m8_latency.json`의 필수 키는 `m8_contract`가 정의한 것을 따른다.
- 테스트: 동시성 1 강제 / 예열 제외 / `rerank_s` P95가 nearest-rank와 일치 / 합산이 manifest의 M7 값을 읽어 계산(문서에 값 하드코딩 없음) / 합산 문구 포함 / 잠금·재실행 / 원본 해시 기록.

### 3. `make m8-report` — `docs/results/2026-10-03-m8-rerank.md`
`m7_report.py` 방식의 생성기다(`<!-- BEGIN generated -->`~`END` 영역은 생성, '사용자 보존 영역'은 유지). 읽기 전에 **원본 해시·행 수를 기록과 대조**한다(`m7_rawhash.verify_fingerprint` 방식, 원본이 로컬에 있을 때; 어긋나면 보고서 생성 거부, FP-053). 생성 영역:
1. 핵심 요약: H8 판정 문구(+ 판정 보류일 때 "n=21이라 판정 보류가 나올 수 있고 그 경우에도 결과를 그대로 보고한다"), 차이·95% CI·n.
2. 사다리: M5 사다리 표(`m5_test_results/run-01.json`·M5 결과 manifest에서 읽음, M5 문서는 바꾸지 않음) 아래에 **+리랭커** 행을 M8 결과로 이어 붙인다(골드 21·실버 65 각각, 이전 단계 대비 Δ와 CI). 대조군(구조 청킹 최종 구성)이 고정 청킹보다 NDCG가 낮다는 M5 사실을 그대로 둔다.
3. 보조 지표(M8-H8b 전 항목), 순위 악화 문항 수, Recall@50 상한, d1 문항 표, dev 격자 6행(`m8_dev.json`)과 선택 행, dev 대조군 값(기록만)과 한 줄 주석("M5 dev 격자는 고정 청킹 기준이라 구조 청킹 대조군의 dev 값은 M5에 기록이 없다. M8에서 처음 기록한다." — `m8_dev.json`의 `baseline_note`에서 읽는다), max_length 규칙·P50/P90/P99/max·dev·test 잘린 쌍 비율.
4. 지연(M8-P2) 표와 보수적 합산 문구, 환경 기록.
5. 실행 정보(FP-044): 고정 실행일(KST) `2026-10-03`, 생성일, 생성·측정 코드 커밋, `config_hash`(m5·m8), 규칙 절 SHA-256, 모델 id·revision·라이선스·파일 크기, 장치·패키지 버전(sentence-transformers·torch·transformers; `uv.lock`에서 읽음), 첫 실행과 재실행 이력(있으면 모두).
6. 범위 밖 사유(M8-N1): 답변 재평가·small-to-big(d2=0)·코퍼스 v2·tools 연동 미적용. "H8이 채택이어도 답변 수준 효과는 측정하지 않았다."
7. '사용자 실행 절차' 목록과 '한계(사용자 보존 영역)'.
- 값을 문서에 손으로 옮기지 않는다. 숫자는 manifest에서 읽는다.
- 테스트: fixture manifest로 생성 영역 핵심 수치(차이·CI·harm 수·P95·합산)가 일치 / 보존 영역 유지 / 원본 해시 불일치 시 거부 / M5 문서를 바꾸지 않음 / 판정 문구가 세 가지 중 하나.

### 4. Makefile·AGENTS.md
- `.PHONY`에 `m8-test m8-latency m8-report`를 추가한다. `m8-test`·`m8-latency`는 `M8_RERUN_FLAGS = $(if $(RERUN_REASON),--rerun-reason "$(RERUN_REASON)")`(step 5가 이미 정의했으면 재정의하지 않고 그대로 쓴다) 형식으로 M5와 같게, `uv run --group embed python -m kodocs.eval.m8_test $(M8_RERUN_FLAGS)` / `m8_latency`, `m8-report`는 `uv run python -m kodocs.eval.m8_report`.
- `AGENTS.md` `## 명령어`에 세 줄을 추가한다: `make m8-test`(test 1회 실행, 재실행은 `RERUN_REASON="사유" make m8-test`, make up·freeze 필요, 사용자 실행), `make m8-latency`(113문항 검색+재정렬 지연, 동시성 1, LLM 없음, 사용자 실행), `make m8-report`(M8 결과 문서 생성, 네트워크 없음). 그리고 사용자 실행 순서 한 줄: `make m8-test → make m8-latency → make m8-report → make readme → make m8-audit`.
- 이 step이 끝나도 결과가 없는 상태에서 `make verify`와 README 최신 여부 테스트(step 4)가 통과해야 한다(H8 '미실행'). 사용자가 실행한 뒤에는 `make readme`를 실행해 README를 갱신하고 커밋해야 `make verify`가 통과한다.

### 5. 결과 계약 수정 — `config_hash` 의미 통일 (`src/kodocs/eval/m8_contract.py`, step 4 산출물)
확인된 사실: 현재 `m8_contract.py`는 결과의 `config_hash`를 `m8_config.json` **파일 바이트**의 SHA-256과 비교한다(`_sha(root / FREEZE_PATH)`, 약 190행). 그런데 step 7의 freeze는 내부에 "나머지 필드의 정규 JSON SHA-256"인 `config_hash`를 넣는다. 같은 이름에 뜻이 둘이다. M5~M7과 같은 뜻으로 통일한다: **`config_hash`는 freeze 내부의 정규 JSON 해시**다.
- a. `m8_contract` 수정: 결과의 `config_hash`는 `m8_config.json` 안의 `config_hash` 필드와 비교한다. 결과에 `config_file_sha256`(`m8_config.json` 파일 바이트의 SHA-256) 키를 추가해 파일과도 비교한다. **둘 다 일치해야 결과로 인정**하고 하나라도 다르면 `H8ResultError`(오류)다. `REQUIRED` 키 목록과 모듈 docstring을 갱신한다(`config_file_sha256` 추가).
- b. step 4의 계약·README 테스트 fixture(`tests/eval/test_m8_contract.py`, `tests/eval/test_m8_readme.py`)를 이 계약에 맞게 고친다: fixture freeze 파일이 내부 `config_hash` 필드를 갖고, 결과 fixture가 `config_hash`·`config_file_sha256`을 둘 다 갖게 한다. 세 상태(미실행/결과/오류) 테스트는 유지하고, 오류 사례로 **"내부 해시 불일치"**(`config_hash`만 다름)와 **"파일 해시 불일치"**(`config_file_sha256`만 다름)를 각각 추가한다.
- c. `m8_test`는 결과에 두 값을 모두 기록한다(위 1번 7항): `config_hash`는 `verify_config`로 검증한 freeze 내부 값, `config_file_sha256`은 실행 시점의 파일 SHA-256이다. 결과 payload가 수정된 `validate_result`를 통과하는 테스트를 둔다.
- 현재 README는 H8 '미실행'이라 이 수정으로 **README가 바뀌지 않아야 한다.** 수정 후 `make readme`를 실행하고 `git diff --stat README.md`가 비어 있는지(멱등) 확인해 보고한다.

## Acceptance Criteria

```bash
make verify   # ruff + pytest 통과(fake로만, 결과 파일이 없는 상태에서 통과해야 한다)
make -n m8-test m8-latency m8-report   # 타깃이 선언돼 있는지만 확인(실행하지 않는다)
make readme   # 한 번 실행한 뒤 `git diff --stat README.md`가 비어 있는지 확인(계약 수정이 README를 바꾸지 않아야 한다)
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

- `make m8-test`·`m8-latency`·`m8-report`를 실행하지 마라(리랭커 로딩·OpenSearch 조회 포함). 이유: 사용자 실행이다. test는 사전 등록 규칙상 1회뿐이라 에이전트가 시험 삼아 실행하면 잠금이 소진된다. 재실행에는 `RERUN_REASON`이 필요하다.
- 대조군 일치 검사·인덱스 검사를 통과하기 전에 리랭커를 로드하거나 결과를 쓰지 마라. 이유: 후보가 M5와 다르면 비교가 무효다. 검사 실패 시 어떤 출력도 남기지 않는다.
- 결과 JSON·원본 jsonl·manifest에 청크·질문 원문을 넣지 마라. 이유: 원문은 비커밋이다(ADR-002). 후보·점수 캐시는 `data/interim/m8/`에만 둔다.
- 결과를 보고 판정 문구·판정 대상(21문항)·부트스트랩 시드·튜닝 설정을 바꾸지 마라. 이유: 사전 등록 M8-H8·T2. 판정 보류가 나와도 그대로 보고하고 해석을 덧붙여 채택처럼 쓰지 마라.
- 지연에 목표(판정)를 만들지 마라. M7 P95 값을 문서에 손으로 쓰지 마라. 이유: M8-P2(기술 통계만, 합산은 두 P95를 단순히 더한 근사이고 분포 합성이 아니다). M7 값은 manifest에서 읽는다.
- 원본 해시를 사후에 계산해 기록하지 마라. 이유: FP-053: 실행 원본(`data/interim/**`)이 비커밋이라 해시는 **실행 시점(완료·실패·정지 시)**에 커밋 manifest에 남겨야 한다. 이후 분석·보고 전에 현재 원본을 기록과 대조한다.
- M5 결과 문서·manifest·로컬 `data/interim/m5/run-01/results.json`을 수정하거나 `make m5-test`를 다시 실행하지 마라. 사다리의 리랭커 행은 M8 문서에만 둔다. 이유: 확정 결과 소급 변경 금지(M5 config_hash 고정).
- 리랭커를 `tools`·`search_docs`·`agent`에 연결하지 마라. 이유: M6 고정 설정·도구 계약을 보호한다. H8이 채택돼도 답변 수준 효과는 측정하지 않았다(M8-N1).
- 결과 계약(`m8_contract.py`)의 키 이름을 5번 수정 외에 임의로 바꾸지 마라. 이유: step 4의 README 생성기와 `load_h8_state`가 같은 계약을 읽는다. 키가 필요하면 계약 모듈을 먼저 수정하고 두 쪽 테스트를 함께 고친다.
- `config_hash`를 `m8_config.json` 파일 바이트의 SHA-256으로 계산해 쓰지 마라. 이유: 이름이 같은데 의미가 둘이 되면 README 검증이 항상 실패한다. 내부 `config_hash`(정규 JSON 해시)와 `config_file_sha256`(파일 해시)을 구분해 기록한다.
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 5·7·8이 만드는 코드가 **사용자 실행 시** 쓰는 `data/interim/m8/`(재현 원시 출력·후보·점수 캐시)만 허용되며 이 경로도 커밋하지 않는다. 에이전트가 step 안에서 이 경로에 쓰지는 않는다(실행은 사용자).
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
