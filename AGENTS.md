# 프로젝트: ko-docs-agentic-rag

> 이 파일이 에이전트 규칙의 단일 원본이다. Codex는 AGENTS.md를, Claude Code는 이 파일을 import하는 CLAUDE.md를 읽는다.

한국어 사내 문서(가상 IT 서비스 회사)를 대상으로 한 Agentic RAG와 평가 하네스. 목표·범위는 `docs/PRD.md`, 구조는 `docs/ARCHITECTURE.md`, 결정 근거는 `docs/ADR.md`, 지표는 `docs/EVAL.md`.

## 기술 스택
- Python 3.12, uv
- 검색: OpenSearch 3.x (로컬 Docker, nori는 `analysis-nori` 플러그인, k-NN·neural-search는 기본 번들, hybrid 쿼리 + RRF·정규화 search pipeline), SQLite(티켓·결재 정형 데이터)
- 임베딩: BGE-M3 (로컬)
- 에이전트: LangGraph (+ langchain-openai 모델 래퍼, `@tool`, `ToolRuntime`)
- 수집: httpx(법제처 Open API·ALIO, 속도 제한), olefile(HWP 5.x 판정)
- 파싱: pypdf, Docling, 업스테이지 Document Parse, LibreOffice + H2Orestart(HWP → PDF), fontconfig 대체 규칙(`infra/fontconfig/fonts.conf`) + 함초롬 글꼴(사용자 설치, 저장소 미포함)
- LLM: Anthropic API(합성 데이터 생성), OpenAI API(시스템 답변), Gemini API(답변 채점)
- MCP 서버: streamable HTTP (데모용 정적 토큰)
- 데모 GUI: Streamlit
- 테스트·린트: pytest, ruff

## 아키텍처 규칙
- CRITICAL: `src/kodocs/retrieval/`은 `langchain`·`langgraph`를 import하지 않는다. 검색은 측정 대상이므로 추상화 없이 OpenSearch·SQLite를 직접 호출한다. 이 경계는 테스트로 강제한다.
- CRITICAL: `agent`·`mcp_server`는 `retrieval`을 `tools`를 통해서만 호출한다. 이 경계는 계층 가드 테스트로 강제한다.
- CRITICAL: 모든 검색은 `retrieval`의 `authorized_*` 함수를 거친다. 권한 필터는 검색 **전에** 적용한다(pre-filter). LangGraph 도구와 MCP 서버는 이 함수를 호출하는 얇은 래퍼일 뿐, 권한 로직을 따로 두지 않는다.
- CRITICAL: 사용자 컨텍스트(팀·직급)는 도구 인자로 받지 않는다. LangGraph는 `ToolRuntime` context로, MCP 서버는 bearer 토큰으로 서버가 결정해 주입한다. 모델이 권한 값을 보거나 만들 수 없어야 한다.
- CRITICAL: `run_sql`은 SELECT만 허용하고, 허용 테이블 목록과 LIMIT을 강제하며, 읽기 전용 연결을 쓴다.
- CRITICAL: 외부 LLM 호출은 `src/kodocs/llm/` 래퍼를 통해서만 한다. 모델명과 생성 설정은 설정에서 읽고 호출 결과에 함께 기록한다. 샘플링 파라미터(temperature 등)는 모델이 지원할 때만 설정·기록하고, 지원하지 않는 모델은 `null`로 기록한 뒤 thinking·effort 설정을 기록한다.
- CRITICAL: `data/golden/`(질의셋·qrels·사람 채점)은 사람이 검수해 확정한 기준이다. 에이전트는 수정하지 않는다. 수치를 맞추려고 golden을 바꾸지 않는다.
- CRITICAL: `data/raw/`(다운로드한 원문)는 커밋하지 않는다. 출처·해시·라이선스는 `data/manifest/`에 기록한다.
- CRITICAL: `data/interim/`(정제 산출물)도 원문을 담으므로 커밋하지 않는다. 법제처 응답의 담당자명·전화번호는 정제 산출물과 manifest에 넣지 않는다. 법제처 인증키(`OC`)는 로그·manifest·예외 메시지에 남기지 않는다.
- CRITICAL: 합성 문서는 가상 회사 이름으로만 만든다. 실존 기관·인물 이름으로 회의록·티켓 같은 기록을 생성하지 않는다. 실제 규정은 기관명 없이 규정명·조항만 참조한다.
- 합성 문서에는 생성 모델, 프롬프트 템플릿 해시, 랜덤 시드, 출처가 된 시드 데이터 사실 ID를 메타데이터로 남긴다.
- CRITICAL: MCP 도구의 입출력 스키마를 호환되지 않게 바꿀 때는 도구 버전을 올린다. 계약 테스트가 이를 강제한다.
- CRITICAL: `make m6-tokens`가 만든 평문 토큰은 gitignore된 파일에만 두고 출력·커밋·트레이스에 남기지 않는다.
- CRITICAL: 실제 API를 호출하는 스크립트는 누적 비용을 기록하고, 설정한 월 상한을 넘기 전에 멈춘다.
- 정답(qrels)은 청크 ID가 아니라 구간(`doc_id` + 조항 ID 또는 시드 데이터 사실 ID·문자 범위)으로 표시한다.
- 식별자는 적재 시 정규식으로 추출해 분석하지 않는 `keyword` 필드에 넣는다.
- 실험은 한 번에 한 변수만 바꾼다. 비교 결과표에는 고정한 조건을 함께 적는다.

## 개발 프로세스
- CRITICAL: 새 기능 구현 시 반드시 테스트를 먼저 작성하고, 테스트가 통과하는 구현을 작성할 것 (TDD)
- CRITICAL: `make verify` 가 유일한 검증 진입점이다. 검증 항목을 추가할 때는 Makefile 의 verify 타깃에 넣는다.
- CRITICAL: 테스트는 LLM·외부 API 호출을 mock한다. `make verify`는 API 키 없이 통과해야 한다.
- CRITICAL: 실제 API를 호출하는 명령(`make generate`, `make eval` 등)은 하네스 step에서 실행하지 않는다. 사용자가 직접 실행한다.
- CRITICAL: `.env` 파일을 열거나 출력하지 않는다. 필요한 변수 이름은 `.env.example`에 적는다.
- 커밋 메시지는 conventional commits 형식을 따를 것 (feat:, fix:, docs:, refactor:)

## 명령어
make readme  # README 생성 영역 갱신(manifest에서 생성, H8은 결과 없으면 '미실행', 결과 흔적이 있는데 검증 실패면 오류) — 네트워크 없음
make ui  # Streamlit 데모 GUI 기동(OpenSearch·API 키 필요, 실제 호출 시 과금) — 사용자 실행
make adr-index  # ADR 상태·상호 참조 색인 생성 → docs/ADR-INDEX.md — 네트워크 없음
make m8-parse-error  # M7 지연 원본 해시 대조·parse_error 오프라인 분류 → data/manifest/m8_parse_error.json — 네트워크 없음
make m8-wrap-report  # M7 환경 정정·parse_error·별표 규정 단위 상한 보고 → docs/results/2026-10-03-m8-wrap-up.md — 네트워크 없음
make m8-audit  # 공개 전환 점검(작업 트리·git 이력·슁글 겹침·이메일 도메인·해시 인벤토리) → data/manifest/m8_audit.json·docs/results/2026-10-03-m8-public-readiness.md — 네트워크 없음
make m8-audit-gh  # GitHub 이슈·PR·댓글 점검(gh 읽기 전용, 건수·위치만) — 네트워크, 사용자 실행
make verify      # ruff + pytest (Stop hook, execute.py, CI가 모두 이 명령을 부른다)
make up          # OpenSearch 기동 (docker compose, 이미지 빌드 포함)
make down        # OpenSearch 중지
make logs        # OpenSearch 로그 추적
make test-integration  # 실행 중인 OpenSearch가 필요한 테스트 — make verify에서는 제외된다, 사용자 실행
make collect-law    # 법제처 Open API 규정 수집(현행·연혁) — 네트워크 호출, 사용자 실행
make collect-alio   # ALIO 내부규정 원본 파일 수집(전 버전, zip 해제) — 네트워크 호출, 사용자 실행
make match-versions # ALIO 파일 ↔ 법제처 버전 발령일자 매칭 (네트워크 없음)
make convert-hwp    # HWP/HWPX → PDF (LibreOffice + H2Orestart 로컬 도구 필요, 실행 시 점검), 사용자 실행
make parse-targets  # M2 파싱 대상 목록(현행 전부 + 과거 버전 일부, 예상 페이지 수) (네트워크 없음)
make parse-pypdf     # M2 pypdf 기준선 파싱 (로컬, 네트워크 없음), 사용자 실행
make docling-models  # Docling 그룹 설치(torch 포함 대용량)·모델 다운로드 — 네트워크, 사용자 실행
make parse-docling   # M2 Docling 파싱 (로컬, 시간이 오래 걸릴 수 있음), 사용자 실행
make parse-upstage-dry  # 업스테이지 예상 비용(캐시 없는 대상의 쪽수 × 단가) 출력만 — 네트워크 없음
make parse-upstage      # 업스테이지 Document Parse(standard) 파싱 — 실제 API 호출·과금, 확인 후 실행, 사용자 실행
make parse-upstage-direct-dry  # 보조 실험: 업스테이지 HWP/HWPX 직접 입력 예상 비용 — 네트워크 없음
make parse-upstage-direct      # 보조 실험: 업스테이지 HWP/HWPX 직접 입력 파싱 — 실제 API 호출·과금, 사용자 실행
make parse-score     # 파서별 파싱 결과를 조문 분할 후 법제처 조문과 대조(분할기 상한 포함) — 네트워크 없음, 사용자 실행
make table-drafts  # HWPX 원본 표(hp:tbl)에서 파서 중립 표 정답 초안 추출 → data/interim/table_drafts/
make table-golden  # 사람이 확정한 data/golden/tables/*.md의 선정 정보·해시를 data/manifest/table_golden.json에 기록
make table-score   # 파서별 표 셀 일치율 채점
make parse-report    # M2 파싱 결과표를 docs/results/{실행일}-m2-parsing.md에 생성·갱신 — 네트워크 없음
make reg-index   # 법제처 현행 규정의 규정명·조 번호·조 제목 색인 → data/manifest/regulation_refs.json (조문 본문 없음, 네트워크 없음)
make seed-names  # 시드 인물 명단을 팀별로 출력(사람이 훑어보고 널리 알려진 인물과 겹치는 이름을 찾는 용도, 네트워크 없음)
make build-db     # 시드 → 티켓·결재 SQLite(data/synthetic/ledger.sqlite, 비커밋) 결정론 빌드, 해시·행 수는 data/manifest/synth_db.json (네트워크 없음)
make plan-docs    # 시드 → 문서 200건 생성 계획·카나리아·인젝션 레지스트리(data/synthetic/, 커밋) (네트워크 없음)
make generate-dry   # 합성 문서 생성 예상 토큰·비용(Batch)만 출력 — 네트워크 없음
make generate-pilot # 파일럿 10건 Batch 제출/수거 — 실제 API 호출·과금, 확인 후 실행, 사용자 실행
make generate-pilot-rerun # 이미 시도한 문서(파일럿)만 바뀐 프롬프트로 새 시도 제출/수거 — 실제 API 호출·과금, 사용자 실행
make generate       # 합성 문서 Batch 생성(재실행 시 조회·수거) → 수거 후 검증·지표·결과 문서 — 실제 API 호출·과금, 사용자 실행. 템플릿이 바뀐 유형의 통과본까지 다시 만들 때 REGENERATE_TYPES=incident (generate-dry도 같음)
make synth-validate # 생성 결과 결정론 검증 → data/synthetic/docs/·validation_failures.jsonl·data/manifest/synth_validation.json (네트워크 없음)
make synth-metrics  # 합성 문서 중복·다양성·분포·사실 커버리지 → data/manifest/synth_metrics.json (네트워크 없음)
make synth-report   # M3 결과 문서 docs/results/2026-09-27-m3-synthetic.md(실행일과 관계없이 고정) 갱신 (네트워크 없음)
make review-sample  # 검증 통과 문서에서 유형·권한 층화 표본 20건 + 검수 양식 → data/synthetic/review/ (네트워크 없음)
make review-score   # data/golden/synthetic_review/*.csv 사람 검수 결과 집계 → data/manifest/synth_review.json·결과 문서 7절 (네트워크 없음)
make query-specs    # 합성 문항 명세(골드 합성 30·실버 138) → data/synthetic/queries/specs.jsonl. 기존 명세가 있으면 선택은 고정하고 qrels·미표시 후보·답 없음 확인만 현재 문서로 재계산, 조건을 어긴 문항만 같은 id로 교체(옛 초안 무효화), 커밋된 `data/synthetic/queries/qrels_overrides.jsonl`(사람이 확인한 문서 단위 qrel 보정, 대상 앵커 일치 필수)을 적용; 처음부터 다시 뽑으려면 `uv run python -m kodocs.eval.golden_specs --reselect` (네트워크 없음)
make queries-dry   # 합성 문항 질문 문장 동기 호출 예상 토큰·비용(동기 단가, mode와 함께 data/manifest/golden_dry_runs.jsonl에 기록) — 네트워크 없음
make queries       # 합성 문항 질문 문장 동기 생성(동시성 4) → 결정론 검증 → 실패 문항 재생성을 max_attempts까지 한 번에 — 실제 API 호출·과금, 사용자 실행
make queries-batch-dry  # Batch 모드 예상 토큰·비용(대량용) — 네트워크 없음
make queries-batch      # Batch 제출/수거(수천 건 이상 등 대량일 때만, ADR-027) — 실제 API 호출·과금, 사용자 실행
make query-validate  # 수거한 질의 초안 결정론 재검증(답 누수·어휘 겹침 상한·태그) → data/manifest/golden_query_validation.json (네트워크 없음)
make lexical-report  # 수거한 질의 초안의 어휘 겹침 분포·상한 후보별 초과 수 출력(파일 쓰지 않음, 상한 확정용, 네트워크 없음)
make golden-real-candidates  # 실제 규정 문항 후보(조 제목·확정 표·개정 쌍) → data/interim/golden/real_candidates.md (로컬 전용, 네트워크 없음)
make golden-real-check       # data/interim/golden/real_drafts.jsonl 검증(할당·조 키·개정 쌍·답 없음 ③·본문 누출) → 커밋용 투영 data/synthetic/queries/real_projection.jsonl (네트워크 없음)
make golden-split-preview    # dev/test 배정 미리보기(그룹·할당 차이·누수 문서·가설 판정 문항 수) → data/manifest/golden_split_preview.json (네트워크 없음)
make golden-packets  # 골드 50·실버 표본 28 검수 패킷(Claude 판정 선채움, 점검용 더미 3은 검수 대상이 아닌 실버에서). 패킷·CSV는 불투명한 검수 번호 r-NNN만 쓰고 대응표는 data/interim/golden/review_keys.json → data/interim/golden/ (로컬 전용, 네트워크 없음)
make golden-promote  # data/golden/m4_review/*-review.csv 집계 → 확정 골드·실버 data/golden/queries/ + 결과 문서 — 사용자 실행
make m4-report       # M4 결과 문서 docs/results/2026-09-28-m4-golden-set.md(고정) 갱신 (네트워크 없음)
make m5-annex-map    # 업스테이지 표 → 별표 id 매핑(로컬, 네트워크 없음) → data/manifest/m5_annex_mapping.json
make m5-chunking     # 코퍼스 구조 청킹 통계·고정 청크 크기 확정 → data/manifest/m5_index.json (네트워크 없음)
make m5-eval-inputs  # 검색 지표 대상·권한 전제 검사·구간 매핑·answer_repeated → data/manifest/m5_eval_inputs.json (네트워크 없음)
make embed-models    # BGE-M3 모델 다운로드(약 2.3GB) — 네트워크, 사용자 실행, embed 그룹 설치 후
make m5-index-dry    # 색인 청크 수·예상 임베딩 수만 출력 — 네트워크·OpenSearch 없음
make m5-index        # 인덱스 4개 빌드(BGE-M3 임베딩·OpenSearch 적재) — make up 필요, 시간이 오래 걸림, 사용자 실행
make m5-dev          # dev 튜닝 그리드 → data/manifest/m5_dev.json — 사용자 실행
make m5-freeze       # 선택 설정을 data/manifest/m5_config.json에 고정(덮어쓰기 거부) — 사용자 실행
make m5-test         # test 1회 실행(재실행은 RERUN_REASON="사유" make m5-test) — 사용자 실행
make m5-report       # M5 결과 문서 docs/results/2026-09-30-m5-retrieval.md(고정) 갱신 (네트워크 없음)
make m6-temporal  # M6 is_current 우선 처리 전후 순위·비 temporal 순서 변경 문항 보고 → data/manifest/m6_temporal.json — OpenSearch 필요(make up), 사용자 실행 (LLM 없음)
make m6-inputs    # M6 평가 입력 집계(dev 75·H5 풀 13·실버 test 83·실버 방향 29·정답 값 분모 56·게이트·라우터 유형) → data/manifest/m6_eval_inputs.json (네트워크 없음)
make m6-dry       # dev 비교(2모델×2시스템×75문항)와 고정 후 test(골드 30·실버 83 세트별 실행 수·모델별 예상 비용) 예상 토큰·비용(예상·상한) — 네트워크 없음
make m6-dev       # 답변 모델 dev 비교(LIMIT=N 파일럿 가능) — 실제 OpenAI 호출·과금, 사용자 실행
make m6-freeze    # 선택 모델·프롬프트·가드레일을 data/manifest/m6_config.json에 고정(덮어쓰기 거부) — 사용자 실행
make m6-test      # 골드 test 30문항 + 실버 test 83문항(H5 방향 확인 보조)을 같은 고정 설정으로 두 시스템 1회씩 실행, 한 실행 단위·한 잠금(재실행은 RERUN_REASON="사유" make m6-test) — 실제 OpenAI 호출·과금, 사용자 실행
make m6-tokens    # MCP 데모 사용자 토큰 생성(평문은 data/interim/mcp/tokens.json 비커밋, 해시만 data/manifest/mcp_token_hashes.json) — 사용자 실행
make m6-report    # M6 결과 문서 docs/results/2026-10-01-m6-agent-mcp.md(고정) 갱신 — 네트워크 없음
make m6-serve     # MCP 서버 기동(streamable HTTP, 127.0.0.1) — make up 필요, 사용자 실행
make m7-probe  # Gemini 채점 설정 확인(baseline·minimal_check·repeat 3회 실제 호출·과금, 확정값 thinking low·temperature/seed 미전송) → data/manifest/m7_probe.json — 사용자 실행
make m7-cards  # κ 표본 50개 + 오프라인 카드 HTML 생성(로컬 전용, 네트워크 없음) — 사용자 실행
make m7-human-register  # 사람 채점 CSV 검증·해시를 data/manifest/m7_human_grades.json에 기록 — 사용자 실행
make m7-pilot  # 초안 루브릭 파일럿 10건 × 3회 채점 — 실제 Gemini 호출·과금, 사용자 실행
make m7-freeze  # 사람 CSV·파일럿·probe 증빙 확인 후 M7 채점 설정 고정(덮어쓰기 거부) — 사용자 실행
make m7-grade-dry  # M7 채점 호출 수·토큰·비용·페이싱 예상, M7_CREDIT_USD로 크레딧 비교 — 네트워크 없음
make m7-grade  # 고정 설정 Gemini 채점(캐시 재개·원본 해시 기록), M7_CREDIT_USD로 크레딧 상한 — 실제 Gemini 호출·과금, 사용자 실행
make m7-analyze  # κ·H5·Sol 사후·라우터 dev 판정 → data/manifest/m7_analysis.json — 네트워크 없음, 사용자 실행
make m7-evidence-check  # 채점 대상 근거 구성(출처별)·근거 0자 건수(사유별)·SQL 재현 대조 → data/manifest/m7_evidence.json — 로컬 오프라인, 네트워크 없음
make m7-security-set  # 보안 테스트셋 생성 + S6 인젝션 노출 검증 — make up 필요, 인덱스 읽기 전용, LLM 호출 없음, 사용자 실행
make m7-security-dry  # 보안 실행 수·M6 test 평균 기반 예상 비용·상한·월 상한 비교 — 네트워크 없음, 사용자 실행
make m7-security  # 실제 OpenAI 호출·과금 + MCP 서버 필요, 인덱스 읽기 전용, 사용자 실행. make up과 함께 make m6-serve를 별도 터미널에서 실행해 둔다(MCP 재생이 실행 중인 서버에 연결). 순서: make m7-security-set → make m7-security-dry → make m7-security → make m7-leak-review-register. 재실행: RERUN_REASON="사유" make m7-security
make m7-leak-review-register  # 사람 c1 확인 CSV 검증·해시 등록 → data/manifest/m7_leak_review.json — 네트워크 없음, 사용자 실행
make m7-security-rejudge  # 기록된 보안 실행을 결정론으로 재판정(c2 식별자 고유성·도구 호출 오류 분류·S6 노출 확정), 정정 전·후를 data/manifest/m7_security_results.json history에 보존 — 모델 호출·새 검색 없음, 사용자 실행
make m7-latency-dry  # 지연 113 × 2 + 예열 6 실행 수·M6 test 평균 기반 예상 비용·월 상한 — 네트워크 없음
make m7-latency  # 실제 OpenAI 호출·과금, 동시성 1 단독 실행, 사용자 실행. 재실행: RERUN_REASON="사유" make m7-latency
make m7-failures  # M6 test 틀린 문항 결정론 실패 분류(기록 부족은 indeterminate, 재검색·모델 호출 없음) → data/manifest/m7_failures.json — 네트워크 없음
make m7-report  # M7 결과 문서 docs/results/2026-10-02-m7-answer-eval.md(고정) 생성 영역만 갱신 — 네트워크 없음
uv run python -m kodocs.eval.m8_rules  # M8 사전 등록 규칙 ID 수·SHA-256 출력 — 네트워크 없음
# 검색·답변 평가는 make m5-test·make m6-test, 채점은 make m7-grade로 각각 실행한다.
(명령은 해당 마일스톤에서 추가된다. docs/PRD.md 마일스톤 참고)
