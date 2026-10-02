# Step 3: streamlit-ui

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `src/kodocs/agent/answer.py`(`answer(question, user_ctx, *, system, runtime, question_id)`)·`types.py`(`AnswerResult`, `AgentRuntime`, `Citation`)·`trace.py` — 반환값과 트레이스 구조(도구 호출·지연·토큰·비용 필드 위치)
- `src/kodocs/eval/m6_runner.py`·`m7_latency.py`·`m7_security.py` — `AgentRuntime`을 만드는 기존 방식(설정 로딩, `ManagedChat`, 도구 의존성)과 실행 경로의 설정 로딩(`load_settings()`)
- `src/kodocs/config.py` — `load_settings()`·월 상한·API 키 오류 타입(`MissingAPIKeyError` 등)
- `src/kodocs/retrieval/access.py`(`UserContext`)·`src/kodocs/mcp_server/tokens.py`(`_query_users` 방식, 평문 토큰 파일 구조 — **평문 파일은 열지 않는다**)
- `data/manifest/mcp_token_hashes.json` — 구조만 확인(`users[]`의 `label`·`team`·`grade`·`token_sha256`)
- `tests/test_architecture.py` — `kodocs.ui` 계층 규칙(ui가 import할 수 있는 모듈과 금지 모듈, 상대 import 포함)
- `src/kodocs/ui/__init__.py`, `Makefile`(`verify` 타깃, `.PHONY`), `pyproject.toml`(읽기만; `ui` 의존성 그룹이 사용자 설치로 이미 있어야 한다)
- `docs/ADR.md` ADR-021(데모 GUI), `docs/PRD.md` 핵심 기능 6, `docs/results/2026-10-02-m7-answer-eval.md`(비스트리밍 기본의 근거: M7 스트리밍 parse_error)
- Streamlit의 `streamlit.testing.v1.AppTest` — 설치된 패키지 소스를 읽어 사용법(`from_file`, `run`, `timeout`, `session_state`)을 확인한다(FP-011: 기억으로 쓰지 않는다)

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

`answer()`만 호출하는 얇은 Streamlit 껍데기를 만든다. 화면은 두 개다: ① 질의 화면(사용자 전환, 답변, 인용, 도구 호출 트레이스, 비용; 같은 질문을 사용자만 바꿔 허용·거절을 나란히 보여줌) ② 결과표 뷰어. 주 데모는 Claude Code + MCP 장면(README, 사용자 촬영)이고 이 GUI는 보조다(ADR-021).

**사전 조건**: 사용자가 `uv add --group ui streamlit`을 이미 실행했다. 이 step은 패키지를 설치하지 않으며 `pyproject.toml`·`uv.lock`을 수정하지 않는다. `ui` 그룹이나 streamlit이 없으면 `blocked`로 보고한다(`reason`: "사용자가 `uv add --group ui streamlit` 실행 필요").

### 1. 의존성·Makefile
- `uv run`은 기본으로 `dev` 그룹만 동기화하므로 `ui` 그룹의 streamlit은 `make verify`에서 빠진다. 그래서 `Makefile`의 `verify` 타깃의 pytest 줄을 `uv run --group ui pytest -q`로 바꾼다(ruff 두 줄은 그대로). 이 변경은 AGENTS.md "검증 항목을 추가할 때는 Makefile의 verify 타깃에 넣는다"에 따른 것이다. `tests/test_m8_ui_make_targets.py`로 이 줄을 검사한다(다른 verify 줄 불변).
- `.PHONY`에 `ui`를 추가하고 `ui:` 타깃을 만든다: `uv run --group ui streamlit run src/kodocs/ui/app.py`(사용자 실행, 이 step에서 실행하지 않는다).
- `AGENTS.md` `## 명령어`에 한 줄 추가: `make ui  # Streamlit 데모 GUI 기동(OpenSearch·API 키 필요, 실제 호출 시 과금) — 사용자 실행`.

### 2. 사용자 목록 `src/kodocs/ui/users.py`
- `load_demo_users(path=Path("data/manifest/mcp_token_hashes.json")) -> tuple[DemoUser, ...]`: 커밋된 manifest에서 **`label`·`team`·`grade`만** 읽는다. `token_sha256`을 변수·반환값·로그에 넣지 않는다(키 화이트리스트로 필드를 고른다). `data/interim/mcp/tokens.json`은 열지 않는다. GUI는 `answer()`를 in-process로 부르므로 토큰이 필요 없다.
- `DemoUser(label, team, grade)`와 `to_user_context()`(`UserContext`는 `kodocs.tools`가 재노출하는 것을 쓴다 — `tests/test_architecture.py`가 허용하는 import 경로를 따른다).
- 사용자 조합 수는 **저장소에서 읽은 값**이다(현재 manifest 기준 38개 (팀, 직급) 조합, 8팀·직급 1~6으로 예상되지만 테스트에 38을 박지 말고 manifest의 고유 (team, grade) 수와 같은지로 검사한다). 최종 보고 summary에 실제 조합 수를 적는다.
- 테스트: 반환 사용자 수 = manifest의 고유 (team, grade) 수 / 반환 객체에 토큰 해시가 없음 / 소스에 `tokens.json` 경로 문자열이 없음(grep) / 필드가 빠진 manifest는 명확한 오류.

### 3. 서비스 계층 `src/kodocs/ui/service.py`
- `ask(question: str, user: DemoUser, system: Literal["simple", "agentic"]) -> UiAnswer`: `kodocs.agent.answer.answer`를 **in-process, 비스트리밍**으로 호출한다(스트리밍 옵션을 만들지 않는다 — M7에서 스트리밍 parse_error가 4건 있었다). 호출 함수는 모듈 수준 `_answer_fn`으로 참조해 테스트가 monkeypatch한다.
- `UiAnswer`: 답변 텍스트, `refused`, `refuse_reason`, 인용 목록(doc_id·section_key·제목이 트레이스에 있으면), 도구 호출 목록(도구 이름·인자 요약·지연·토큰), 모델 이름, 총 비용(USD, 트레이스 값), 전체 지연. 필드 위치는 `trace.py`와 실제 트레이스 JSONL 한 줄을 읽어 확인한다(FP-011).
- 런타임 생성은 기존에 `AgentRuntime`을 만드는 코드(M6·M7 러너)를 **재사용**한다. `kodocs.ui`가 `retrieval`·`tools`를 직접 import해야 하면 계층 규칙 위반이므로 `kodocs.agent` 쪽에 공개 팩토리(`build_default_runtime()` 등)를 두고 ui는 그것만 부른다(`tests/test_architecture.py`를 통과해야 한다). 런타임 생성은 **실제 `load_settings()` 경로**(`.env` 로딩)를 쓴다. 키 주입 mock만으로 테스트하지 마라 — 테스트는 임시 `.env`와 `load_dotenv` 격리로 `load_settings()`가 호출되는지 확인하고 환경 변수를 복원한다(FP-056). 개발자의 실제 `.env`를 읽지 않는다.
- 오류 처리: API 키 없음·월 상한 초과·OpenSearch 연결 실패는 화면에 친절한 한글 메시지로 보여주고 트레이스백·환경 변수 값·키를 보이지 않는다. 예외 메시지에 키가 들어갈 수 있으므로 알려진 오류 타입만 메시지 매핑하고 나머지는 일반 오류 문구로 바꾼다.

### 4. 화면 `src/kodocs/ui/app.py` (+ 필요하면 `ui/views.py`)
- 탭 두 개: "질의", "결과표".
- **질의 탭**: 질문 입력, 시스템 선택(simple·agentic, 기본 agentic), 사용자 A·B 선택(팀·직급 selectbox 또는 `label`), "같은 질문을 두 사용자로 실행" 버튼. 두 열에 각 사용자의 답변·거절 여부·인용·도구 호출 트레이스(expander)·비용·모델·지연을 보여준다. 단일 사용자 실행도 지원한다. 실제 호출은 버튼을 눌렀을 때만 일어난다(페이지 로드만으로 API를 호출하지 않는다).
- **거절 화면은 문서의 존재를 암시하지 않는다**(FP-052와 ADR-025의 'indistinguishable_from_no_answer'): 거절·답 없음 응답에 `refuse_reason` 코드나 권한 부족 문구를 덧붙여 "권한이 없어서"처럼 보이게 하지 않는다. 시스템이 돌려준 답변 텍스트를 그대로 보이고, 표시를 위한 내부 라벨(예: 가드레일 종류)은 트레이스 expander 안에만 둔다. 트레이스에는 해당 사용자의 도구 결과만 있다.
- 세션 비용 누적 표시(화면 상단): 이번 세션에서 실행한 호출의 비용 합(트레이스 값 합). 월 상한 검사는 기존 `ManagedChat`이 하므로 UI가 따로 만들지 않는다.
- **결과표 탭**: `docs/results/*.md` 목록에서 선택해 `st.markdown`으로 렌더링한다(기본 목록에서 `*-appendix.md`는 제외하고 체크박스로 포함). 읽는 것은 `docs/results/`와 `data/manifest/`뿐이다. 마크다운 상대 링크가 안 열려도 괜찮다.
- 화면 문구는 한국어다.

### 5. 테스트 (`tests/ui/`)
- **Streamlit 설치 확인 테스트(조용히 건너뛰기 금지)**: `pytest.importorskip`·`skipif`·try/except로 건너뛰지 마라. `test_streamlit_is_installed`는 `import streamlit`이 실패하면 "사용자가 `uv add --group ui streamlit` 실행 필요"라는 메시지로 **실패**해야 한다. AppTest를 쓰는 테스트 모듈은 최상단에서 `from streamlit.testing.v1 import AppTest`를 import한다(없으면 수집 단계에서 실패한다).
- AppTest + mock `answer`(`kodocs.ui.service._answer_fn` monkeypatch): ① 같은 질문·다른 두 사용자에서 허용 사용자는 답변·인용이, 다른 사용자는 거절 응답이 나란히 표시된다 ② 거절 응답 화면에 인용·`doc_id`·권한 부족 문구가 없다 ③ 트레이스 expander에 도구 이름이 보인다 ④ 비용(USD)이 표시되고 세션 누적이 맞다 ⑤ 버튼을 누르기 전에는 `answer`가 호출되지 않는다 ⑥ API 키 없음 오류가 한글 메시지로 표시되고 값이 노출되지 않는다.
- 계층·소스 검사: `kodocs.ui`가 `retrieval`·`tools`(UserContext 재노출 제외, architecture 테스트가 허용하는 경로)·`mcp` 서버를 직접 import하지 않는다 / UI 코드에 `stream=True`·`.stream(`·`astream` 같은 스트리밍 호출이 없다 / UI가 `os.environ`·`.env`를 직접 읽지 않는다(`load_settings()` 경유만).
- 결과표 뷰어: 임시 `docs/results` fixture로 목록·appendix 제외·렌더링을 검사한다.
- 모든 테스트는 LLM·외부 API·OpenSearch를 호출하지 않는다(mock). `make verify`는 API 키 없이 통과해야 한다.
- 실제 호출 데모와 스크린샷·GIF는 사용자가 한다. 이 step에서 `make ui`를 실행하지 않는다.

## Acceptance Criteria

```bash
make verify   # ruff + pytest(AppTest 포함, `--group ui`로 실행) 통과
make -n ui   # 타깃이 선언돼 있는지만 확인(실행하지 않는다)
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

- Streamlit이 없을 때 AppTest를 건너뛰거나 try/except·`importorskip`로 숨기지 마라. 이유: 조용한 건너뛰기는 GUI가 검증되지 않은 채 `make verify`가 통과하게 만든다. 설치 안 됐으면 테스트가 실패해야 한다.
- `streamlit`을 직접 설치하거나 `pyproject.toml`·`uv.lock`을 수정하지 마라. 이유: 설치는 사용자가 한다(`uv add --group ui streamlit`). 없으면 `blocked`.
- `data/interim/mcp/tokens.json`(평문 토큰)을 열거나 그 경로를 코드에 쓰지 마라. `mcp_token_hashes.json`의 `token_sha256`도 읽지 마라. 이유: AGENTS.md CRITICAL('평문 토큰은 gitignore된 파일에만, 출력·커밋·트레이스에 남기지 않는다'). GUI는 in-process라 토큰이 필요 없다.
- UI에서 스트리밍 호출을 쓰거나 `retrieval`·MCP를 직접 호출하지 마라. 이유: 최종 답변 호출은 비스트리밍이 기본이다(M7 스트리밍 parse_error 4건), UI는 `answer()`만 호출하는 얇은 껍데기다(ADR-021, 계층 가드 테스트).
- UI에서 권한 값(팀·직급)을 모델이 보거나 만들 수 있게 하지 마라. 이유: 사용자 컨텍스트는 서버(`answer()` 호출 인자)가 결정한다(AGENTS.md CRITICAL). 사용자 선택은 UI 입력으로만 `UserContext`를 만들고 프롬프트에 넣지 않는다.
- 거절 응답에 권한 부족·문서 존재를 암시하는 문구를 추가하지 마라. 이유: FP-052·ADR-025(권한 밖 응답은 답 없음과 구별되면 안 된다).
- 사용자 조합 수를 추측해 코드·테스트에 쓰지 마라(32·38 등). 이유: 저장소(manifest)에서 읽은 값만 쓴다. M7 보안은 그중 32명만 골라 썼을 뿐 GUI 목록과 같은 값이 아니다.
- 설정 로딩을 키 주입 mock으로만 테스트하지 마라. 이유: FP-056(env 격리 결함을 놓침). 단 테스트가 개발자의 실제 `.env`를 읽지 않게 `load_dotenv`를 막거나 임시 `.env`로 바꾼다.
- 이 step에서 `make ui`를 실행하거나 실제 모델 호출을 하지 마라. 이유: 실제 호출·과금은 사용자 실행(ADR-014).
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 6·7이 만드는 `data/interim/m8/` 캐시 쓰기만 허용되며 이 경로도 커밋하지 않는다.
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
