# Step 9: mcp-server

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/tools/`(`run_tool`·`contract.py`·`TOOL_VERSIONS`·`ToolDeps`·`build_tool_deps`), `tests/contract/snapshots/`, `tests/tools/test_contract.py`
- `src/kodocs/mcp_server/__init__.py`(현재 비어 있음), `src/kodocs/agent/answer.py`(in-process 경로, parity 비교 대상), `data/golden/queries/*.jsonl`(질문자 `asker.team`·`asker.grade` 분포 — 읽기만), `data/synthetic/canaries.json`, `data/synthetic/docs/`(문서 access 메타데이터 — 읽기만)
- `tests/test_architecture.py`, `tests/test_integration_index_names.py`, `tests/test_m5_opensearch_integration.py`, `.gitignore`
- `pyproject.toml`, `Makefile`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

MCP 서버(streamable HTTP, bearer 토큰)와 데모 토큰 생성기를 만든다. **이 step에서만** `uv add mcp`를 허용한다. 설치 후 **설치된 공식 MCP Python SDK의 실제 API**(서버 클래스, streamable HTTP 앱 생성, 도구 목록·호출 핸들러, 구조화 출력·`isError`, 클라이언트 세션)를 소스로 확인하고 그에 맞춰 구현한다(추측 금지, FP-011). 필요한 것이 SDK에 없으면 `blocked`로 보고한다.

### 1. 토큰 (`mcp_server/tokens.py`, `make m6-tokens`)
- 데모 사용자 = 골드·실버 질의의 distinct `asker`(team, grade) 쌍 전부 + 라벨 `demo-{team}-g{grade}`. 사용자별로 `secrets.token_urlsafe(32)` 토큰을 만든다.
- **평문 토큰은 `data/interim/mcp/tokens.json`(gitignore된 경로, `.gitignore`의 `data/interim/`가 덮는지 테스트로 확인)에만 쓴다. 저장소에는 sha256 해시만** `data/manifest/mcp_token_hashes.json`(`{"users":[{"label","team","grade","token_sha256"}]}`)에 쓴다. 명령의 stdout·stderr·로그·예외 메시지·트레이스에 평문 토큰을 절대 출력하지 않는다(개수·라벨만 출력). 두 파일 중 하나라도 있으면 덮어쓰지 않고 멈춘다(`--rotate`가 있을 때만 새로 만든다).
- 서버는 해시 파일만 읽는다. 검증은 `hmac.compare_digest`(상수 시간)로 한다.
- `Makefile`에 `m6-tokens`, `m6-serve` 타깃(`verify`에는 넣지 않음)과 `AGENTS.md` 명령어 줄을 추가한다: `make m6-tokens    # MCP 데모 사용자 토큰 생성(평문은 data/interim/mcp/tokens.json 비커밋, 해시만 data/manifest/mcp_token_hashes.json) — 사용자 실행` / `make m6-serve     # MCP 서버 기동(streamable HTTP, 127.0.0.1) — make up 필요, 사용자 실행`. `tests/test_m6_make_targets.py`에 선언 테스트를 추가한다.

### 2. 서버 (`mcp_server/`)
- 도구 4개를 `tools.contract`의 이름·설명·**입력 JSON 스키마 그대로** 노출한다(자동 추론 스키마가 아니라 계약 스키마여야 스냅샷과 같다). 각 도구에 버전을 `_meta`(또는 SDK가 허용하는 위치)로 표시한다. 도구 호출은 `tools.run_tool(name, args, ctx, deps)`만 부르는 얇은 래퍼다. `retrieval`을 import하지 않는다.
- 인증: 모든 MCP 요청(도구 목록 조회 포함)에서 `Authorization: Bearer <token>`을 검증한다. 토큰 없음·형식 오류·무효는 **동일한** HTTP 401과 본문 `{"error":{"code":"unauthorized","message":"인증에 실패했습니다."}}`(`WWW-Authenticate: Bearer`)이다. 어떤 실패인지 구분하지 않는다.
- 사용자 컨텍스트: 검증된 토큰 → `UserContext(team, grade)`를 요청 단위로만 보관한다(`contextvars` 등). **전역 변수·모듈 상태에 사용자를 두지 않는다.** 서로 다른 두 사용자의 동시 요청이 섞이지 않는지 테스트한다.
- 결과: `ToolResult.payload`를 그대로 구조화 출력으로 돌려주고, 오류 payload면 `isError=true`로 표시한다. 문서 단위 권한 부족은 `not_found`(step 4의 바이트 동일 응답)이다.
- 실행: `python -m kodocs.mcp_server`가 `127.0.0.1`에 바인딩해 기동한다(포트는 인자, 기본 8765). 기동 시 `build_tool_deps()`로 실제 OpenSearch·임베더를 준비하고 임베더 로딩 시간은 로그에 별도로 남긴다. **테스트는 실제 기동·네트워크 없이 ASGI 앱을 in-process로 호출한다.**

### 3. 테스트
- 계약: 서버가 노출한 도구 이름·입력 스키마·설명이 `tests/contract/snapshots`의 최신 스냅샷과 같고 도구 4개 버전이 `TOOL_VERSIONS`와 같음.
- 인증: 토큰 없음·무효·형식 오류가 같은 401 본문, 유효 토큰은 통과, 해시 파일에 평문이 없음, `make m6-tokens` 출력에 평문이 없음(`capsys`), 이미 있으면 덮어쓰지 않음.
- **parity(규칙 19)**: 같은 (사용자, 도구, 인자)에 대해 `tools.run_tool`의 payload와 MCP 도구 호출 결과가 (변동 필드를 제외하고) `json.dumps(sort_keys=True)`로 동일함. 4개 도구 × 정상·오류(`invalid_argument`·`not_found`) 케이스.
- 권한(fake client): MCP 경로의 모든 검색·조회 요청 본문에 `access_filter`(질문자 팀·직급)가 들어 있음, 사용자 A·B 동시 요청 분리, 사용자 인자를 넘겨도(`team` 등) `invalid_argument`.
- 통합 테스트(`tests/test_m6_permission_integration.py`, `@pytest.mark.integration`, 사용자 실행): 실제 OpenSearch의 M5 실측 인덱스를 **읽기 전용**으로 쓴다. 제한(restricted) 합성 문서 3개(카나리아 포함)와 접근 권한이 없는 사용자 3종을 골라, 도구 4개 × 경로 2개(in-process `run_tool`, MCP in-process 앱) 각각에서 (a) 결과에 제한 문서 ID가 없고 (b) 카나리아 문자열이 없고 (c) `get_document`가 없는 ID와 바이트 동일한 `not_found`를 내는지 확인한다. 이 모듈은 `build_index`·삭제·인덱스 생성 호출을 하면 안 되며, 정적 검사 테스트(`tests/test_integration_index_names.py` 확장)가 이를 막는다. 이 step에서는 실행하지 않고 파일·정적 가드만 만든다.
- 계층 가드: `kodocs.mcp_server`가 `kodocs.retrieval*`·`kodocs.agent`·`openai`·`langchain*`를 import하지 않음(위반 fixture 포함).

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
uv run pytest -q tests/mcp_server tests/tools tests/test_architecture.py
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - 평문 토큰이 저장소 추적 파일·stdout·로그·예외 메시지·트레이스 어디에도 없는가?
   - 인증 실패 401이 실패 원인과 무관하게 동일한가, 서버 전역에 사용자 상태가 없는가?
   - in-process와 MCP의 payload parity 테스트가 4개 도구 모두에서 통과하는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 평문 토큰을 출력·로그·커밋·트레이스에 남기지 마라(생성 직후 화면 출력도 금지). 이유: 사용자 요구 사항이다. 평문은 gitignore된 파일에만 두고 저장소에는 해시만 남긴다.
- 토큰 비교에 `==`를 쓰지 마라. 이유: 타이밍 차이로 토큰이 추측될 수 있다(`hmac.compare_digest`).
- MCP 서버에서 권한 로직을 구현하거나 `retrieval`을 직접 부르지 마라. 이유: 서버는 `tools.run_tool`을 부르는 얇은 래퍼이며 권한은 `retrieval.authorized_*` 한 곳에만 있다(ADR-010·011).
- `0.0.0.0`에 바인딩하거나 실제로 서버를 기동·네트워크 접속하는 테스트를 만들지 마라. 이유: 데모용 정적 토큰 서버이며 테스트는 ASGI in-process다.
- 요청 사용자 정보를 모듈 전역·싱글턴에 저장하지 마라. 이유: 동시 요청에서 다른 사용자의 권한으로 검색되는 권한 누설이 된다.
- 실측 인덱스를 삭제·생성하는 통합 테스트를 만들지 마라. 이유: FP-046.
- 이 step 외 의존성을 추가하지 마라(`mcp`만 허용). 이유: FP-040.
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
