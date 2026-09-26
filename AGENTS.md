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
- CRITICAL: 모든 검색은 `retrieval`의 `authorized_*` 함수를 거친다. 권한 필터는 검색 **전에** 적용한다(pre-filter). LangGraph 도구와 MCP 서버는 이 함수를 호출하는 얇은 래퍼일 뿐, 권한 로직을 따로 두지 않는다.
- CRITICAL: 사용자 컨텍스트(팀·직급)는 도구 인자로 받지 않는다. LangGraph는 `ToolRuntime` context로, MCP 서버는 bearer 토큰으로 서버가 결정해 주입한다. 모델이 권한 값을 보거나 만들 수 없어야 한다.
- CRITICAL: `run_sql`은 SELECT만 허용하고, 허용 테이블 목록과 LIMIT을 강제하며, 읽기 전용 연결을 쓴다.
- CRITICAL: 외부 LLM 호출은 `src/kodocs/llm/` 래퍼를 통해서만 한다. 모델명·temperature는 설정에서 읽고 호출 결과에 함께 기록한다.
- CRITICAL: `data/golden/`(질의셋·qrels·사람 채점)은 사람이 검수해 확정한 기준이다. 에이전트는 수정하지 않는다. 수치를 맞추려고 golden을 바꾸지 않는다.
- CRITICAL: `data/raw/`(다운로드한 원문)는 커밋하지 않는다. 출처·해시·라이선스는 `data/manifest/`에 기록한다.
- CRITICAL: `data/interim/`(정제 산출물)도 원문을 담으므로 커밋하지 않는다. 법제처 응답의 담당자명·전화번호는 정제 산출물과 manifest에 넣지 않는다. 법제처 인증키(`OC`)는 로그·manifest·예외 메시지에 남기지 않는다.
- CRITICAL: 합성 문서는 가상 회사 이름으로만 만든다. 실존 기관·인물 이름으로 회의록·티켓 같은 기록을 생성하지 않는다. 실제 규정은 기관명 없이 규정명·조항만 참조한다.
- 합성 문서에는 생성 모델, 프롬프트 템플릿 해시, 랜덤 시드, 출처가 된 시드 데이터 사실 ID를 메타데이터로 남긴다.
- CRITICAL: MCP 도구의 입출력 스키마를 호환되지 않게 바꿀 때는 도구 버전을 올린다. 계약 테스트가 이를 강제한다.
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
make generate    # 합성 코퍼스 생성 — 실제 API 호출, 사용자 실행
make eval        # 검색·답변 평가 — 실제 API 호출, 사용자 실행
(명령은 해당 마일스톤에서 추가된다. docs/PRD.md 마일스톤 참고)
