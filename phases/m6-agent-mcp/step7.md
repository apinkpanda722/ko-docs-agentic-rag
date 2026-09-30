# Step 7: simple-rag

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/agent/`(step 6: `types.py`·`guardrails.py`·`evidence.py`·`finalize.py`·`prompts.py`·`trace.py`), `src/kodocs/tools/`(`run_tool`·`contract.py`)
- `src/kodocs/llm/openai_chat.py`(`ManagedChat`), `tests/agent/`, `tests/tools/test_contract.py`
- `pyproject.toml`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

LangGraph 도구 래퍼와 **단순 RAG 그래프**를 만든다. **이 step에서만** `uv add langgraph langchain`을 허용한다(`langchain-openai`가 이미 `langchain-core`를 끌어오므로 설치된 것을 먼저 확인하고, `ToolRuntime`이 들어 있는 패키지·버전을 **설치된 실제 소스로 확인**해 필요한 것만 추가한다. 이름·위치를 기억으로 추측하지 마라 — FP-011). `context_schema`·`ToolRuntime`·`invoke(..., context=...)`의 실제 시그니처도 설치본에서 확인한다. 이 API를 쓸 수 없으면 `blocked`로 보고한다.

### 1. 공통 도구 실행 경로 `agent/tools.py`
- `AgentContext`(dataclass): `user: UserContext`(서버가 정한 값), `evidence: EvidenceLog`, `guard: Guard`, `trace`(트레이스 세션). 그래프는 `context=AgentContext(...)`로 호출한다.
- `execute_tool_call(ctx: AgentContext, deps: ToolDeps, name: str, args: dict) -> ToolResult`: (1) `ctx.guard.register_tool_calls(1)`, (2) `tools.run_tool(name, args, ctx.user, deps)`, (3) 결과를 `EvidenceLog`·트레이스 단계(`kind="tool"`, `latency_ms`, `refs`)에 기록. **도구를 실행하는 모든 경로(단순 RAG의 검색, Agentic의 도구 호출)가 이 함수 하나를 거친다.**
- `build_langgraph_tools(deps) -> list[BaseTool]`: 도구 4개를 `@tool`(이름·설명·`args_schema`는 `tools.contract`의 것 그대로)로 만든다. 함수는 `runtime: ToolRuntime`(context = `AgentContext`)을 받고 `execute_tool_call`을 부른 뒤 payload를 JSON 문자열로 돌려준다. **`ToolRuntime` 인자는 모델이 보는 스키마에 나타나면 안 되고, 모델에게 보이는 인자 스키마는 `tools.contract`의 입력 모델 JSON 스키마와 같아야 한다**(같음을 테스트로 검증). 권한 값을 도구 인자로 받지 않는다.

### 2. 단순 RAG 그래프 `agent/simple_rag.py`
- LangGraph `StateGraph`. 노드 `retrieve` → `answer`. `retrieve`: `execute_tool_call(..., "search_docs", {"query": <질문 원문 그대로>})` **1회**(질문 재작성·식별자 추출·`lookup_id`·`run_sql` 없음, 모델 도구 바인딩 없음). `answer`: `evidence.round_robin_context(budget=6_000)`로 근거 텍스트를 만들고 `prompts.build_final_messages`로 메시지를 구성해 `chat.invoke(messages, response_schema=ModelAnswer)`를 **1회** 호출한다. 호출 전에 `guard.check_before_model_call()`을 확인하고 호출 후 `guard.add_usage`. `ChatParseError` → `refused, parse_error`. 가드레일 위반 → 해당 `refuse_reason`. 결과는 `finalize_answer`를 거친다.
- 시스템 타임아웃은 `simple_timeout_s=45`(마감은 `Guard`가 관리).
- 그래프는 `build_simple_graph(runtime: AgentRuntime)`로 만들어 컴파일한다(`answer()` 진입점은 step 8).

### 3. 테스트 (fake chat·fake deps)
- 도구 스키마: 모델에 보이는 인자에 `runtime`·`team`·`grade`·`user`가 없고, 계약 입력 스키마와 동일. 계약에 없는 인자(`team` 등)를 넣으면 `invalid_argument`(계약 입력 모델이 `extra="forbid"`).
- `ToolRuntime` 주입: 도구가 호출될 때 `run_tool`에 전달된 `ctx`가 그래프 `context`의 `UserContext`와 동일 객체 값임을 fake deps로 확인.
- 단순 RAG: 검색 정확히 1회, 질문이 원문 그대로 전달됨, 모델 호출 정확히 1회, 모델에 도구가 바인딩되지 않음, 컨텍스트가 8청크·6,000자 이내, `prefer_current`가 켜진 상태로 검색됨, 거절·인용 검증·트레이스 1줄 기록.
- 두 사용자(팀 A·B)를 번갈아 호출해도 컨텍스트가 섞이지 않음.

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
uv run pytest -q tests/agent
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - 단순 RAG가 검색 1회·모델 호출 1회만 하는가(규칙 4: 재작성·다른 도구 없음)?
   - 도구 실행 경로가 `execute_tool_call` 하나뿐인가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `retrieval`을 직접 import하거나 권한 조건을 `agent/`에 구현하지 마라. 이유: `agent`는 `tools`를 통해서만 검색한다(계층 가드, ADR-029 2번).
- 질문을 재작성하거나 식별자를 추출해 검색하지 마라. 이유: 규칙 4는 단순 RAG의 검색 질의를 사용자 질문 원문으로 고정한다. 차이를 도구·반복 하나로 좁혀야 H5가 해석된다.
- 이 step에서 `langgraph`·`langchain` 외 패키지(`mcp` 등)를 추가하지 마라. 이유: 의존성은 승인된 step에서만 추가한다.
- `ToolRuntime`·`context_schema` API를 문서·기억에 의존해 쓰지 마라. 이유: 설치된 실제 버전으로 확인한다(FP-011).
- 사용자 컨텍스트를 그래프 state나 프롬프트에 넣어 모델이 보게 하지 마라. 이유: 서버가 `context`로 주입하며 모델은 볼 수 없어야 한다(CRITICAL).
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
