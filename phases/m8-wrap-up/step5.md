# Step 5: ui-streaming

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `src/kodocs/llm/openai_chat.py` — `ManagedChat.invoke`(재시도 루프, `stream: bool`)와 `_invoke_once`의 스트리밍 루프, `ChatParseError`, `_classify_error`(전송 후 실패는 재시도하지 않음 — FP-035), `ChatResult` 필드, `tests/llm/`의 기존 테스트
- `src/kodocs/agent/types.py`(`AgentRuntime`, `ChatLike`, `AnswerResult`)·`answer.py`·`agentic_rag.py`·`simple_rag.py`·`tools.py`(도구 실행 래퍼, `add_tool_step` 호출부)·`trace.py`(`add_model_step`·`add_tool_step`·`_sanitize`, 트레이스 필드와 `request_id`가 있는지)·`finalize.py`·`runtime.py`
- `src/kodocs/ui/`(step 3: `service.py`·`app.py`·`users.py`·`views`)와 `tests/ui/`·`tests/test_m8_ui_make_targets.py` — **비스트리밍 강제 테스트**(UI 코드에 `stream=True`·`.stream(`·`astream`이 없음을 검사하는 것)를 이 step에서 바꾼다
- `src/kodocs/eval/m7_latency.py` — `StreamingChat`(최종 구조화 호출만 `stream=True`), 지연 질문 목록·실행·잠금·`RERUN_REASON`·원본 지문, `src/kodocs/eval/m7_rawhash.py`
- `src/kodocs/eval/m8_parse_error.py`·`m8_wrap_report.py`·`m8_readme.py`(step 2·4)와 `data/manifest/m8_parse_error.json`·`docs/results/2026-10-03-m8-wrap-up.md`·`README.md`
- `data/manifest/costs.jsonl` — 비용 원장. **실제 몇 줄을 읽어** 필드(`run_id`·`request_id`·`output_tokens`·`reasoning_tokens`·`amount_usd` 등)와 한 요청이 여러 줄로 나뉘는지 확인한다(FP-011). `logs/traces/2026-10-02.jsonl`(비커밋 로컬)과 `data/interim/m7/latency/runs.jsonl`의 실제 한 줄에서 `request_id`가 있는지, parse_error 행의 필드를 확인한다
- `src/kodocs/costs.py`(`BudgetGuard`·원장)·`src/kodocs/config.py`(`load_settings`)·`src/kodocs/eval/m6_config.py`·`m7_config.py`·`src/kodocs/llm/answer_settings.py` — 고정 설정 해시 계산·검증 방식
- `docs/results/2026-10-02-m7-answer-eval.md`('지연 한계 2(스트리밍 parse_error)') — 건수와 현재 서술
- **설치된 패키지 소스**(FP-011: 기억으로 쓰지 않는다): `langchain_openai/chat_models/base.py`(스트리밍 청크의 `response_metadata`·`status`·`incomplete_details`, 구조화 출력의 strict 전송 여부), `langchain_core`의 부분 JSON 파서(`parse_partial_json` 또는 동등 함수), `langgraph/prebuilt/tool_node.py`(`ToolNode`의 병렬 실행 방식), `streamlit`(스크립트 스레드와 `ScriptRunContext` 제약, 작업 스레드에서 `st.*` 호출 가능 여부, `st.status`·`st.empty` 갱신 API)
- `docs/results/hypotheses.md` 'M8 판정 규칙' — **수정 금지**(규칙 절 해시가 `m8_rules.rules_sha256()`으로 보존된다). 재현 보고 규칙은 wrap-up 문서 템플릿에 둔다
- `tests/test_architecture.py`·`tests/agent/`·`tests/contract/`·`tests/tools/` — 계층 규칙, 에이전트·도구 테스트 fixture(권한 비노출 테스트가 재사용한다)

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

세 가지 결정에 따라 UI를 바꾸고, parse_error 원인 분류를 원장으로 보강하고, 스트리밍 폭주 재현 실행 코드를 만든다. 이 step은 **코드·테스트·생성 문서 갱신**까지다. 실제 API 호출과 재현 실행(`make m8-stream-repro`)은 사용자가 한다.

### 배경(조언 세션이 확인한 사실 — 코드가 센 값과 대조하라)
- M7 지연 측정(`run_id = m7-latency`)의 원장 `data/manifest/costs.jsonl`을 `request_id`로 묶으면 **529건**이다. 그중 정확히 **4건**이 `output_tokens = 4000`(= `max_output_tokens`, `reasoning_tokens` 0)이고, 지연 17.9·23.6·19.9·18.0초가 parse_error 4행(s-syn-050·s-syn-139 simple, g-syn-022·s-syn-034 agentic)과 대응한다.
- 같은 실행의 나머지 출력은 최대 472토큰이다. 비스트리밍 실행(m6-dev 1,507건, m6-test-01 516건, m7-security 999건)은 최대 780토큰이고 4000 도달은 0건이다.
- 설치된 langchain-openai는 스트리밍에서도 pydantic 스키마를 strict json_schema로 보낸다(`chat_models/base.py`의 `_construct_responses_api_payload`). 따라서 "스트리밍은 strict가 아니다"는 원인이 아니다.
- 결론: 4건은 **출력 폭주로 상한에 도달해 JSON이 잘린 것**이다. **왜 길어졌는지는 원시 출력이 없어 미확정**이다.
- 결정: GUI는 실제 모델 스트리밍(최종 구조화 답변의 `answer` 필드)과 도구 호출 실시간 표시로 간다. step 3의 "비스트리밍 기본" 결정은 철회한다.
위 숫자는 기대값이다. 코드가 센 값이 다르면 둘 다 최종 보고에 적고 manifest에는 코드가 센 값만 쓴다. 이 숫자를 테스트에 박지 마라(fixture 원장으로 검사한다).

### A. parse_error 재분류 (`src/kodocs/eval/m8_parse_error.py`, `make m8-parse-error`)
- 분류에 원장 근거 범주 **`max_output_tokens_reached`("max_output_tokens 도달(JSON 잘림)")**를 추가한다. 기존 범주·`indeterminate` 규칙은 유지한다.
- **원장 연결**: parse_error 행(M7 지연 `runs.jsonl`)을 원장 요청에 잇는 1순위 방법은 trace의 `request_id`다. trace에 `request_id`가 없으면 **실행 순서와 지연**으로 연결한다(같은 질문·시스템·모드 순서, 지연 오차 범위를 코드 상수로 두고 manifest에 적는다). 어떤 방법으로 이었는지를 행마다 `link_method`(`request_id` | `order_and_latency` | `unlinked`)로 manifest에 쓴다. 연결을 확정할 수 없으면 `indeterminate`이며 추측으로 연결하지 않는다(FP-054의 교훈: 키가 요청 전체를 가리키지 않으면 다른 요청을 연결한다).
- 원장을 `request_id`로 묶는 방법(한 요청이 여러 줄이면 `output_tokens`를 어떻게 합치는지)은 실제 원장 줄을 읽고 정의해 manifest `definition`에 적는다(FP-043: 무엇을 세는지). 529건을 코드가 센다.
- **4건 각각의 근거를 기록한다**: `output_tokens`, `max_output_tokens`(M6 고정 설정에서 읽음), `reasoning_tokens`, 지연(초). 비교 근거로 **비스트리밍 실행(m6-dev·m6-test-01·m7-security)의 `run_id`별 건수와 최대 `output_tokens`**, 같은 실행(`m7-latency`)의 나머지 요청 최대 `output_tokens`와 4000 도달 건수를 같이 쓴다. 값은 원장에서 코드가 센다.
- `cause_note`(manifest와 wrap-up 문서 모두): "왜 길어졌는지는 원시 출력 미보존으로 미확정. 스트리밍 여부 자체는 원인이 아님(스트리밍에서도 strict 구조화 스키마를 전송함 — 설치된 langchain-openai 소스 확인)." 원인을 추측해 쓰지 마라.
- M7 manifest·원장(`costs.jsonl`)은 **읽기 전용**이다(수정 금지). 출력은 `data/manifest/m8_parse_error.json`(M8 소유, 재생성 가능)이다. wrap-up 문서(`m8_wrap_report.py`)와 README 한계(`m8_readme.py`)는 생성기로 갱신한다(손으로 고치지 않는다).
- 테스트: fixture 원장·fixture trace로 범주 부여(4000 도달 → 새 범주, 미달 → 기존 규칙), 연결 방법별 `link_method`, 연결 불가 시 `indeterminate`, 비교 통계 집계, `cause_note` 존재, 출력에 답변 본문 필드 없음.

### B. 최종 답변 실제 스트리밍
1. **`ManagedChat.invoke`**(`src/kodocs/llm/openai_chat.py`)에 선택 인자 `on_text_delta: Callable[[str], None] | None = None`을 추가한다(`ChatLike` 프로토콜도 같이). `None`이면 **동작이 지금과 완전히 같아야 한다**. 스트리밍 루프(`_invoke_once`의 `stream` 분기)에서 **텍스트 조각만** 콜백으로 넘긴다(청크의 텍스트 블록만; usage·reasoning·메타 청크는 넘기지 않는다). 콜백이 예외를 던져도 호출 결과가 바뀌지 않게 감싼다(콜백 오류가 답변 실패가 되면 안 된다). 재시도는 전송 전 실패와 429·5xx에서만 일어나고 전송 후 실패는 재시도하지 않으므로(`_classify_error` — 소스로 확인) 텍스트 조각이 재시도로 중복되지 않는지 확인하고 테스트한다.
   - 회귀 테스트: `on_text_delta=None`이면 기존 M6·M7 경로 테스트가 그대로 통과하고, **고정 설정 해시(`m6_config`·`m7_config`)가 바뀌지 않음**(현재 해시 계산 함수로 재계산해 커밋된 값과 같다)을 테스트한다.
2. **UI 스트리밍 래퍼**(`src/kodocs/ui/streaming.py`)는 M7 `StreamingChat` 방식을 따른다: **최종 구조화 답변 호출(`response_schema`가 있는 호출)만** `stream=True` + `on_text_delta`로 돌리고 도구 선택 턴은 그대로 둔다. `answer()` 시그니처와 계약은 바꾸지 않는다. 래퍼는 UI가 넘긴 콜백을 최종 호출에만 붙인다.
3. **화면**: 스트리밍되는 JSON 텍스트에서 `ModelAnswer.answer`를 **부분 JSON으로 추출**해 실시간 표시한다. 부분 JSON 파서는 설치된 `langchain_core` 소스를 읽어 확인한 함수(예: `parse_partial_json`)를 쓴다(escape·미완성 문자열 처리를 직접 짜지 않는다). 완료되면 finalize된 `AnswerResult`로 교체한다. **최종 결과가 거절이면 스트리밍된 글자를 지우고 거절 응답을 그대로 보인다**(ADR-025: 권한 밖 응답은 답 없음과 구별되면 안 된다 — 거절 전에 보인 글자가 남으면 안 된다).
4. **폭주 대비**:
   - 스트리밍 실패(`ChatParseError`) 시 **응답 status, `incomplete_details` 사유, `output_tokens`, 출력 문자 길이**를 trace와 로그에 남긴다. **답변 본문·원시 출력은 남기지 않는다.** 값을 얻는 위치(마지막 청크의 `response_metadata` 등)는 설치된 `langchain_openai` 소스로 확인한다. `ChatParseError`에 진단 필드(`diagnostics`)를 달고(문자열 본문 없음) trace의 해당 단계에 기록하는 방식이면 `trace.py` 기존 필드 계약을 깨지 않게 선택 필드로 추가한다.
   - 화면에는 "**응답이 비정상적으로 길어져 중단됐습니다. 다시 시도해 주세요**"와 재시도 버튼을 둔다. 재시도는 같은 질문·사용자·시스템으로 다시 스트리밍한다.
   - **비스트리밍으로 조용히 바꾸지 않는다.**
5. **step 3의 비스트리밍 강제 테스트를 바꾼다**: "UI 코드에 `stream=True`·`.stream(`·`astream`이 없다"를 **"스트리밍은 최종 답변 호출에만, `ui/streaming.py` 래퍼를 통해서만"**으로 바꾼다(UI의 다른 모듈에서 직접 스트리밍 호출·`stream=True`가 없고, 래퍼는 `response_schema`가 있는 호출에만 스트리밍을 켠다).

### C. 도구 호출 실시간 표시 (필수)
**a. 이벤트 경로 — `answer()` 계약은 불변.**
- `AgentRuntime`에 선택 필드 `event_sink: Callable[[Mapping[str, object]], None] | None = None`을 추가한다. `None`이면 동작이 지금과 같고 **고정 설정 해시도 불변**이어야 한다(테스트: 해시 함수가 `AgentRuntime`의 새 필드를 포함하지 않음).
- 이벤트를 내는 지점: **도구 시작**(`agent/tools.py` 도구 실행 래퍼, 실행 직전) / **도구 완료**(같은 곳, `add_tool_step` 직후) / **모델 단계 시작·완료**(`agentic_rag.py`·`simple_rag.py`의 `add_model_step` 주변) / **최종 답변 시작**(최종 구조화 호출 직전).
- 이벤트 필드: `kind`(`model_start`/`model_end`/`tool_start`/`tool_end`/`final_start`), 도구 이름, 인자(trace와 **같은 `_sanitize` 결과**), 지연(ms), 오류 코드, refs 수, 단계 번호.
- **결과 본문·문서 제목·근거 텍스트·질문 원문 이외의 사용자 데이터는 넣지 않는다. 가드레일 거절 사유는 실시간 이벤트로 보내지 않는다**(trace expander에만 있다). 이벤트는 읽기 전용 스냅샷(불변 매핑)으로 만든다.
- 이벤트 발행은 실패해도 답변에 영향이 없어야 한다: 싱크가 예외를 던져도 삼키고 카운트만 한다(테스트).

**b. 스레드 — 소스로 확인한 뒤 쓴다(FP-011).**
- LangGraph `ToolNode`의 병렬 도구 실행 방식(스레드풀 여부·한 턴에 여러 도구 호출 시 동시 실행)을 설치된 langgraph 소스로, Streamlit 스크립트 스레드 제약(`st.*`를 작업 스레드에서 부를 수 있는지, `ScriptRunContext`)을 설치된 streamlit 소스로 확인하고 근거 줄을 코드 주석에 남긴다.
- `answer()`는 **작업 스레드**에서 돌린다. `event_sink`와 `on_text_delta`는 **같은 `queue.Queue`에 넣기만** 한다(큐 외 Streamlit API 호출 금지). 스크립트 스레드가 큐를 읽어 `st.status`(또는 확인한 동등 API)와 답변 영역을 갱신한다. 작업이 끝나면 완료 신호(센티널)를 큐에 넣는다.
- 작업 스레드 예외는 스크립트 스레드로 전달해 **기존 한글 오류 매핑**(step 3 `service.py`)을 쓴다. 키·환경 변수 값이 메시지에 들어가지 않게 알려진 타입만 매핑하는 기존 규칙을 유지한다.
- **두 사용자 비교 실행은 순차**로 한다(첫 사용자 완료 후 두 번째). 스레드를 겹쳐 실행하지 않는다.

**c. 화면**: 도구별로 "실행 중 → 완료(지연, refs 수)" 또는 "오류(코드)"를 한 줄씩 보인다. 병렬 도구는 동시에 "실행 중"으로 보인다. 모델 단계는 "다음 행동 결정 중"으로 표시한다. 최종 답변이 시작되면 텍스트 스트리밍(B)으로 넘어간다. 완료 후 상태 목록은 접어서 남긴다.

**d. 권한 비노출 테스트(필수)**: 같은 도구 호출(예: `get_document` 또는 `lookup_id`)에서 (가) **권한 없는 사용자가 실제 존재하는 비공개 문서를 조회**한 경우와 (나) **존재하지 않는 문서 ID를 조회**한 경우를 비교한다. 두 경우의 **이벤트(`tool_start`·`tool_end` 필드 전부)와 trace tool step이 오류 코드·refs 수·`result_chars`까지 같아야 한다**(ADR-029 8번: 문서 단위 권한 부족은 없는 ID와 바이트 동일한 `not_found`). 기존 `tests/agent`·`tests/tools`·`tests/contract`의 fake 의존성과 fixture를 재사용한다. **다르면 임의로 패딩하지 말고 `blocked`로 보고한다**(기존 보안 판정 H6와 관련된 문제다; 어떤 필드가 다른지 적는다).

### D. 폭주 재현 실행 코드 (`src/kodocs/eval/m8_stream_repro.py`, `make m8-stream-repro` — 실행은 사용자)
- **고정 설정**: M6 고정 설정(gpt-6-luna, `reasoning.effort = "low"`, `max_output_tokens = 4000`)을 그대로 쓴다. 현재 설정 해시가 `data/manifest/m6_config.json`과 다르면 중단한다. 최종 호출은 `ui/streaming.py`가 아니라 M7 `StreamingChat`과 같은 방식(최종 구조화 호출만 `stream=True`)이다.
- **1단계(기본)**: 질문 `s-syn-050`·`s-syn-139`(simple), `g-syn-022`·`s-syn-034`(agentic)를 **원래 시스템·원래 asker**로 실행한다(질문 ID로 `data/golden/queries/*.jsonl`에서 읽는다). 질문마다 **스트리밍 25회 + 비스트리밍 25회 = 총 200회**, **동시성 1**, 고정 시드(`20261003`)로 순서를 섞어 두 모드를 **교차**시킨다(모드가 시간대에 몰리지 않게). 교차 순서가 시드에서 재현되는 테스트를 둔다.
- **2단계(`--stage 2`, 1단계 폭주 0건일 때 사용자가 선택)**: M7 지연과 같은 113문항 × simple·agentic을 스트리밍 1회씩(226회). 대상은 `data/manifest/m7_latency.json`에서 읽는다. 1단계 완료 기록이 없으면 거부하고, 1단계 폭주 건수를 출력한다.
- **기록**:
  - 비커밋 `data/interim/m8/repro/`: 실패·폭주(`output_tokens ≥ 4000` 또는 parse 실패) 건의 **원시 출력 전문**. 스트리밍은 `on_text_delta`로 누적한 텍스트, 비스트리밍은 응답 메시지 본문을 쓴다. 원시 파일은 `git check-ignore` 대상이어야 한다.
  - 커밋 `data/manifest/m8_stream_repro.json`: 실행별로 질문 ID·시스템·모드·`status`·`output_tokens`·`reasoning_tokens`·incomplete 사유·지연. **폭주 건은 형태 통계만** 커밋한다: 잘릴 때 생성 중이던 JSON 필드, 필드별 문자 길이, 공백·개행 비율, 가장 긴 반복 부분 문자열 길이와 반복 횟수, citations 항목 수. **본문·원시 출력 조각은 커밋하지 않는다.**
  - 원시 파일의 SHA-256·행 수는 **실행 시점(완료·실패·정지 시)**에 기록한다(FP-053). **한 번만 실행하는 잠금**을 두고(단계별), 재실행에는 `RERUN_REASON`(사유·날짜·이전 결과 해시 누적)이 있어야 한다.
- **dry-run(`make m8-stream-repro-dry`, `--dry-run`)**: 실행 전에 예상 비용을 출력한다. 근거는 원장의 `m7-latency` 요청당 평균 비용과 **폭주 요청(4000 토큰) 비용**이다(평균 기준 추정과, 전량 폭주를 가정한 상한 두 값). `BudgetGuard`의 월 상한 검사를 거치고 예상이 상한을 넘으면 중단한다. dry-run은 네트워크·`.env` 없이 동작한다. 실제 실행 경로는 **실제 `load_settings()`(`.env` 로딩)**를 쓴다(FP-056: 키 주입 mock만으로 테스트하면 env 격리 결함을 놓친다. 테스트는 임시 `.env`와 `load_dotenv` 격리로 확인하고 개발자의 실제 `.env`를 읽지 않는다).
- 실행 중 항목 하나의 실패(API 오류·parse 실패)는 기록하고 계속한다(FP-030). 인증·결제·한도 오류는 전체를 멈춘다(FP-035: 전송 후 실패는 재시도하지 않는다 — `ManagedChat` 규칙을 따른다). 진행 줄을 N건마다 `flush=True`로 출력한다(FP-034, 키·URL 없음).
- **보고 규칙(결과 보기 전 고정 — `m8_wrap_report.py`의 wrap-up 문서 템플릿에 둔다. `hypotheses.md` M8 규칙 절은 해시 보존을 위해 건드리지 않는다)**: 모드별·질문별 폭주 건수와 비율을 **기술 통계로만** 보고한다(가설 검정 없음). 폭주가 0건이면 "재현 안 됨(1단계 200회[, 2단계 226회])"로 쓴다. 원시 출력은 인용하지 않는다. 결과 manifest가 없으면 wrap-up 문서는 "재현 실행 미실행"으로 쓴다(결과 manifest가 있는데 해시 검증에 실패하면 오류로 멈춘다 — M8-D1과 같은 규칙).
- Makefile: `.PHONY`에 `m8-stream-repro m8-stream-repro-dry`를 추가한다. `m8-stream-repro:`는 `uv run --group embed python -m kodocs.eval.m8_stream_repro $(M8_RERUN_FLAGS) $(if $(STAGE),--stage $(STAGE))`(`M8_RERUN_FLAGS`가 이미 정의돼 있지 않으면 `M5_RERUN_FLAGS` 방식으로 정의한다), `m8-stream-repro-dry:`는 `--dry-run`. 이 타깃을 이 step에서 **실행하지 않는다**.

### E. 문서·명령
- README의 GUI 설명과 한계에 **"실제 모델 스트리밍(최종 답변), 도구 호출 실시간 표시, 폭주 시 진단 기록·재시도"**를 반영한다. `README.md`를 손으로 고치지 않고 `m8_readme.py`(손으로 쓴 문장 템플릿 또는 생성 로직)를 고친 뒤 `make readme`로 갱신한다. 한계에는 parse_error 4건이 출력 폭주로 잘린 것으로 분류됐고 원인은 미확정이라는 사실을 생성기가 manifest에서 읽어 쓴다(수치는 생성). 손으로 쓴 영역에 결과 수치를 넣지 않는 기존 테스트를 유지한다.
- `AGENTS.md`: `## 명령어`에 `make m8-stream-repro-dry`(예상 비용, 네트워크 없음)와 `make m8-stream-repro`(실제 API 호출·과금, 사용자 실행, 1단계 200회, `STAGE=2`로 2단계 226회, 재실행은 `RERUN_REASON="사유"`)를 추가한다. 아키텍처 규칙에 한 줄을 추가한다: "`event_sink` 이벤트에는 결과 본문·문서 제목·근거 텍스트·가드레일 거절 사유를 넣지 않는다(trace expander에만)."
- 사용자 실행 순서(최종 보고에 적는다): 이 step 완료 → `make up` → `make m8-stream-repro-dry` → `make m8-stream-repro`(1단계) → 필요하면 `STAGE=2 make m8-stream-repro` → `make m8-wrap-report` → `make readme` → 커밋 → 그다음 리랭커(step 6~8).

### F. 테스트 (fake chat·fake runtime·fake 도구, 실제 API 호출 없음)
- **스트리밍**: 부분 표시(부분 JSON에서 `answer` 추출), 최종 교체, 거절 시 교체(스트리밍된 글자가 사라짐), 실패 진단 필드 기록과 본문 없음(trace·로그에 원시 출력 없음), 재시도 버튼, 비스트리밍으로 조용히 바뀌지 않음.
- **회귀**: `on_text_delta=None`과 `event_sink=None`에서 기존 동작 동일, `m6_config`·`m7_config` 고정 설정 해시 불변.
- **이벤트**: 순서(병렬 교차 허용: 같은 턴의 두 `tool_start`가 두 `tool_end`보다 먼저 올 수 있음), 작업 스레드 이벤트가 **스크립트 스레드에서만 렌더링됨**(작업 스레드에서 `st.*` 호출이 없음을 보이는 테스트), 작업 스레드 예외 전달과 한글 오류 매핑, 이벤트에 본문·제목·근거 없음, 가드레일 사유 비노출, **권한 비노출(C-d)**, 싱크 예외가 답변에 영향 없음.
- **AppTest**: 상태 목록(도구별 실행 중→완료/오류)과 최종 답변 표시, 두 사용자 순차 실행, 거절 화면.
- **재현 실행**: 교차 순서 재현성, 폭주 경계(`output_tokens` 3999는 폭주 아님, 4000은 폭주), 형태 통계 계산(공백 비율·반복 부분 문자열·필드별 길이·citations 수를 알려진 fixture 문자열로), 커밋 manifest에 본문 없음, 원시 파일 경로가 `git check-ignore` 대상, 잠금과 `RERUN_REASON`, dry-run(원장 평균·폭주 비용·월 상한 중단), 설정 해시 불일치 시 중단, `STAGE=2`가 1단계 완료 없이 거부, 실제 `load_settings()` 경로(FP-056).
- **parse_error 재분류**: fixture 원장으로 범주와 근거 기록(A).

## Acceptance Criteria

```bash
make verify   # ruff + pytest 통과(fake로만, 실제 API·OpenSearch 호출 없음)
make m8-parse-error && make m8-wrap-report   # 원장 기반 재분류와 wrap-up 문서 갱신(오프라인)
make readme   # README 갱신(오프라인); 한 번 더 실행해 `git diff --stat README.md`가 첫 실행 결과와 같은지(멱등) 직접 확인해 보고
make -n m8-stream-repro m8-stream-repro-dry   # 타깃이 선언돼 있는지만 확인(실행하지 않는다)
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조와 계층 경계(`retrieval`은 langchain·langgraph·tools·agent·mcp_server·ui를 import하지 않는다 등)를 따르는가?
   - ADR 기술 스택을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙을 위반하지 않았는가?
   - 이 step의 `
## 금지사항

- `make m8-stream-repro`·`make m8-stream-repro-dry`를 실행하거나 실제 OpenAI API를 호출하지 마라(테스트는 fake chat). 이유: 재현 실행은 실제 호출·과금이라 사용자가 실행한다(ADR-014, FP-040). 에이전트가 시험 삼아 돌리면 한 번만 실행하는 잠금이 소진된다.
- `answer()`의 시그니처·계약을 바꾸지 마라. `ManagedChat.invoke`의 `on_text_delta=None`·`AgentRuntime.event_sink=None` 동작을 바꾸지 마라. 이유: M6·M7 확정 결과가 이 경로로 만들어졌다. 고정 설정 해시(`m6_config`·`m7_config`)가 바뀌면 M6·M7 입력 검증이 깨진다(회귀 테스트로 강제).
- 이벤트에 결과 본문·문서 제목·근거 텍스트를 넣지 마라. 가드레일 거절 사유를 실시간 이벤트로 보내지 마라. 이유: 실시간 표시는 권한 판단이 끝난 결과를 보여주는 통로라, 문서 존재나 거절 이유를 새게 하면 H6이 말하는 존재 누설이 된다(FP-052, ADR-025).
- 권한 비노출 테스트(C-d)에서 두 경우가 다르면 임의로 패딩·정규화해 같게 만들지 마라. 이유: 응답이 구별되면 보안 구현의 결함이다(ADR-029 8번). `blocked`로 보고하고 다른 필드를 적는다.
- 작업 스레드에서 Streamlit API(`st.*`)를 호출하지 마라. 큐에 넣기만 한다. 이유: 스크립트 스레드 밖의 호출은 컨텍스트 오류나 화면 불일치를 일으킬 수 있다. 소스로 확인한 규칙만 따른다(FP-011).
- 두 사용자 비교를 병렬로 실행하지 마라. 이유: 이벤트·텍스트 조각이 섞이고 비용·월 상한 검사가 꼬인다. 순차로 한다.
- 스트리밍 실패 시 비스트리밍으로 조용히 바꾸지 마라. 이유: 폭주가 가려져 원인 조사가 불가능해진다. 진단을 남기고 사용자에게 재시도를 맡긴다.
- 답변 본문·원시 출력을 trace·로그·커밋 manifest에 남기지 마라(진단 필드와 형태 통계만). 원시 출력은 사용자 실행이 쓰는 `data/interim/m8/repro/`(비커밋)에만 둔다. 이유: 실제 규정 근거가 답변에 섞일 수 있다(ADR-002).
- parse_error 4건의 원인을 추측해 확정하지 마라. 스트리밍 자체를 원인이라고 쓰지 마라. 이유: 원장 근거로는 출력 폭주로 인한 잘림까지만 확인됐고 왜 길어졌는지는 원시 출력 미보존으로 미확정이다. 설치된 langchain-openai가 스트리밍에서도 strict 스키마를 보내므로 "strict가 아니어서"도 원인이 아니다.
- `data/manifest/costs.jsonl`·M7 manifest를 수정하지 마라(읽기 전용). `docs/results/hypotheses.md`의 M8 판정 규칙 절을 건드리지 마라. 이유: 원장은 append-only 비용 기록이고(FP-032), 규칙 절 해시가 `m8_rules.rules_sha256()`으로 보존된다. 재현 보고 규칙은 wrap-up 문서 템플릿에 둔다.
- 원장↔parse_error 연결을 근사로 확정하지 마라. 이유: 연결 방법(`request_id` 또는 순서·지연)을 행마다 기록하고 연결 불가는 `indeterminate`로 둔다(FP-054: 키가 요청 일부만 가리키면 다른 요청을 잘못 연결한다).
- 재현 실행의 설정 로딩을 키 주입 mock으로만 테스트하지 마라. 이유: FP-056. dry-run만 env 격리, 실제 실행은 실제 `load_settings()` 경로를 쓴다.
- 원본 해시를 사후에 계산해 기록하지 마라. 이유: FP-053. 실행 시점(완료·실패·정지 시)에 원시 파일 SHA-256·행 수를 manifest에 남긴다.
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 5·7·8이 만드는 코드가 **사용자 실행 시** 쓰는 `data/interim/m8/`(재현 원시 출력·후보·점수 캐시)만 허용되며 이 경로도 커밋하지 않는다. 에이전트가 step 안에서 이 경로에 쓰지는 않는다(실행은 사용자).
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
