# Step 1: layer-guards

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `tests/test_architecture.py` — 기존 retrieval 가드(AST로 top-level 모듈만 검사)
- `src/kodocs/retrieval/__init__.py`, `src/kodocs/chunking/__init__.py`, `src/kodocs/eval/`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

`tests/test_architecture.py`를 확장한다(필요하면 `tests/architecture_helpers.py` 같은 도우미 모듈을 둔다). 구현 코드는 만들지 않는다.

### 1. 계층 가드 (PRD M5 산출물)

`src/kodocs/retrieval/**/*.py`의 모든 import를 AST로 검사해 아래를 금지한다.
- `langchain*`, `langgraph*` (기존 규칙 유지)
- `kodocs.agent`, `kodocs.mcp_server`, `kodocs.ui` 및 그 하위 모듈
- **상대 import 포함**: 파일 위치를 기준으로 절대 이름으로 풀어 검사한다. 예: `retrieval/x.py`의 `from ..agent import y`, `from .. import agent`, `from ...kodocs import ui`류, `from kodocs import agent`, `import kodocs.mcp_server.foo`.
- 문자열 동적 import(`importlib.import_module("kodocs.agent")`, `__import__("kodocs.ui")`)의 리터럴 인자.
- 함수 안(지연) import도 `ast.walk`로 잡는다.

검사 함수는 `find_forbidden_imports(path: Path, package_root: Path) -> list[str]` 형태로 분리해, **위반 예시를 담은 임시 파일(`tmp_path`)로 검사기 자체를 테스트**한다(각 위반 형태마다 케이스 1개 + 위반 없는 파일 케이스). 이유: 검사기가 아무것도 못 잡아도 통과하는 가드는 무용하다.

### 2. M5 LLM 미호출 가드

아래 대상 파일이 `anthropic`, `openai`, `google`(genai), `kodocs.llm`을 import하지 않는지 검사한다.
- `src/kodocs/retrieval/**`, `src/kodocs/chunking/**`, `src/kodocs/eval/m5_*.py`
- 이후 step이 만드는 M5 평가 모듈은 **파일 이름이 `m5_`로 시작**하는 규칙으로 자동 포함된다. 이 명명 규칙을 테스트 docstring에 적는다.
- 대상 파일이 하나도 없는 패턴(`eval/m5_*.py`)은 통과시키되, 패키지 디렉토리 존재 테스트는 유지한다.

## Acceptance Criteria

```bash
make verify
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
   - 위반 fixture 케이스가 실제로 실패를 감지하는가(검사기 자체 테스트)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 네트워크 요청·다운로드·패키지 설치를 하지 마라. 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 필요해 보이면 `blocked`로 보고한다.
- LLM·외부 API를 호출하거나 `anthropic`·`openai`·`google.genai`·`kodocs.llm`을 import하지 마라. 이유: M5는 결정론 검색 실험 phase이며 LLM 호출이 생기면 멈추고 보고하는 조건이다.
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `src/kodocs/retrieval/`에서 `langchain`·`langgraph`·`kodocs.agent`·`kodocs.mcp_server`·`kodocs.ui`를 (상대 import 포함) import하지 마라. 이유: 검색은 측정 대상이라 추상화 없이 OpenSearch·SQLite를 직접 호출한다(ADR-009).
- 기존 테스트를 깨뜨리지 마라
- 기존 `test_retrieval_does_not_import_langchain_or_langgraph`를 지우지 마라. 이유: 이미 강제 중인 CRITICAL 규칙이다. 확장하되 같은 이름의 회귀를 남긴다.
- top-level 모듈명만 보는 방식으로 되돌리지 마라. 이유: 상대 import(`from ..agent`)는 `node.module`이 `agent`이거나 None이라 기존 방식으로는 놓친다.
