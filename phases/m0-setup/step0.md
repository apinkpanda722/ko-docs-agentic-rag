# Step 0: project-skeleton

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 기술 스택, CRITICAL 규칙, 명령어
- `docs/ARCHITECTURE.md` — 디렉토리 구조 (이 step이 만들 골격의 원본)
- `docs/ADR.md` — 특히 ADR-009(LangGraph는 쓰되 검색 추상화는 쓰지 않는다), ADR-014(실제 API 호출은 사용자가 실행)
- `docs/PRD.md` — 마일스톤 표의 M0 행
- `Makefile` — 현재 `verify` 타깃(하네스 기본값)
- `scripts/README.md`, `scripts/execute.py` — `make verify`를 누가 어떻게 호출하는지
- `.gitignore`

## 배경

이 프로젝트에는 아직 Python 코드가 하나도 없다. `pyproject.toml`, `src/`, `tests/` 모두 없고,
`Makefile`의 `verify` 타깃은 하네스 기본값(`python3 -m pytest -q scripts`)이다.
이 step이 첫 코드이며, uv 프로젝트 골격과 `make verify` 진입점을 확정한다.

## 작업

### 1. 아키텍처 가드 테스트를 먼저 작성한다 (TDD)

`tests/test_architecture.py`를 먼저 만든다. AGENTS.md는 다음을 CRITICAL로 정하고 있고,
"이 경계는 테스트로 강제한다"고 명시한다:

> `src/kodocs/retrieval/`은 `langchain`·`langgraph`를 import하지 않는다.

테스트 시그니처 수준 지시:

```python
def _imported_top_level_modules(path: Path) -> set[str]:
    """파일을 AST로 파싱해 import된 top-level 모듈 이름을 모은다."""

def test_retrieval_package_exists() -> None:
    """가드 테스트가 조용히 무력화되지 않도록 대상 디렉토리 존재를 먼저 확인한다."""

def test_retrieval_does_not_import_langchain_or_langgraph() -> None:
    """src/kodocs/retrieval/ 하위 모든 .py 에서 금지 import를 검출한다."""
```

규칙:
- 금지 판정은 top-level 모듈 이름의 prefix 일치로 한다. `langchain`, `langchain_openai`,
  `langgraph`, `langgraph_sdk` 등 `langchain`·`langgraph`로 시작하는 모든 모듈이 금지다.
  `langchain-openai` 래퍼는 `src/kodocs/agent/`에서만 쓰기 때문에 prefix 일치여야 한다.
- 문자열 grep이 아니라 `ast` 모듈로 파싱한다. 이유: 주석·docstring에 적힌 단어까지 잡으면
  가드가 거짓 양성을 내고, 이후 step에서 무력화될 위험이 있다.
- `import X`(`ast.Import`)와 `from X import Y`(`ast.ImportFrom`) 양쪽을 본다.
- 위반이 있으면 어떤 파일의 어떤 모듈인지 에러 메시지에 담는다.

이 시점에서 테스트를 실행하면 `src/kodocs/retrieval/`이 없어 실패해야 한다. 실패를 확인한 뒤 2번으로 간다.

### 2. uv 프로젝트를 초기화한다

- `pyproject.toml` — 프로젝트명 `kodocs`, src 레이아웃(`src/kodocs/`), `requires-python = ">=3.12"`.
  빌드 백엔드는 uv 기본값을 쓴다.
- `.python-version` — `3.12`
- 의존성은 아래만 넣는다. **다른 패키지를 추가하지 마라.**
  - runtime: `python-dotenv`
  - dev(`[dependency-groups] dev`): `ruff`, `pytest`, `pyyaml`
  - `pyyaml`은 다음 step에서 docker-compose YAML을 파싱해 검증하는 테스트에 쓴다.
- `uv.lock`이 생성되면 커밋 대상이다(삭제하거나 gitignore에 넣지 마라). 재현성 때문이다.

### 3. 디렉토리 골격을 만든다

`docs/ARCHITECTURE.md`의 구조를 그대로 따른다. `src/kodocs/` 아래 9개 패키지를 만들고,
각 패키지에는 `__init__.py` 하나와 그 패키지의 역할을 적은 docstring 한 줄만 둔다.

```
src/kodocs/__init__.py
src/kodocs/{ingest,parsing,chunking,synth,retrieval,agent,mcp_server,llm,ui}/__init__.py
eval/__init__.py
tests/
```

- 구현 코드는 쓰지 마라. 이 step은 골격과 검증 진입점까지다.
- `data/`, `docs/results/`, `logs/traces/`, `infra/`는 이 step에서 만들지 않는다.
  `infra/`는 다음 step, 나머지는 해당 데이터를 만드는 마일스톤에서 생긴다.

### 4. ruff·pytest 설정을 넣는다

`pyproject.toml`에:
- `[tool.ruff]` — 대상에서 `scripts/`를 **제외**한다(`extend-exclude`). 이유: `scripts/` 아래 파일은
  상위 하네스 저장소에서 복사해 온 것이고 이 프로젝트가 소유하지 않는다. 린트 규칙을 맞추려고
  하네스 파일을 수정하면 안 된다.
- `[tool.pytest.ini_options]` — `testpaths = ["tests", "scripts"]`. 이유: `scripts/test_execute.py`는
  하네스 실행기 자체의 테스트이고, 실행기가 깨지면 이후 모든 step이 멈춘다. 검증 대상에 남긴다.
  `scripts/test_execute.py`는 stdlib과 `pytest`만 쓰므로 추가 의존성이 필요하지 않다.

### 5. `make verify`를 교체한다

`Makefile`의 `verify` 타깃을 아래로 바꾼다:

```makefile
verify:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest -q
```

- `verify`는 하네스의 유일한 검증 진입점이다. Stop hook, `scripts/execute.py`, CI, step AC가 모두 이 타깃을 부른다.
- 기존 주석 중 "프로젝트 셋업에서 스택에 맞게 채운다"는 안내 주석은 이제 채웠으니 정리해도 된다.
  `.PHONY` 갱신을 잊지 마라.
- `make verify`는 API 키 없이, Docker 없이, 네트워크 없이 통과해야 한다(AGENTS.md CRITICAL).

### 6. 가드 테스트를 통과시킨다

3번에서 `src/kodocs/retrieval/`이 생겼으므로 1번 테스트가 통과한다. `make verify`로 확인한다.
`ruff format --check`가 실패하면 `uv run ruff format .`으로 포맷을 맞춘 뒤 다시 확인한다.

## Acceptance Criteria

```bash
make verify                                  # ruff check + ruff format --check + pytest 전부 통과
uv run python -c "import kodocs; import kodocs.retrieval; print('ok')"
uv run pytest -q tests/test_architecture.py  # 가드 테스트가 실제로 수집·실행된다
uv run pytest -q scripts                     # 하네스 자체 테스트가 여전히 통과한다
```

## 검증 절차

1. 위 AC 커맨드를 순서대로 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - `docs/ARCHITECTURE.md`의 디렉토리 구조와 패키지 이름이 정확히 일치하는가?
   - `pyproject.toml` 의존성이 `python-dotenv` + dev(`ruff`, `pytest`, `pyyaml`) 넷뿐인가?
   - `AGENTS.md` CRITICAL 규칙을 위반하지 않았는가? (`make verify`가 유일한 진입점, 키 없이 통과)
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `scripts/` 아래 어떤 파일도 수정하지 마라. 이유: 상위 하네스 저장소의 파일이고, `execute.py`는
  자기 자신을 검증하는 테스트를 갖고 있다. 린트 때문에 손대야 할 것 같으면 ruff exclude로 해결한다.
- `opensearch-py`, `langchain`, `langgraph`, `torch`, `sentence-transformers`, `FlagEmbedding`,
  `streamlit`, `docling`, `pypdf` 등을 의존성에 추가하지 마라. 이유: 무거운 의존성은 쓰는
  마일스톤에서 추가한다. 지금 넣으면 M0 검증이 느려지고 설치 실패 지점만 늘어난다.
- `mypy`·`pyright` 같은 타입 체커를 `verify`에 넣지 마라. 이유: AGENTS.md 기술 스택은 린트·테스트를
  `ruff`, `pytest`로 정했다. 검증 항목 추가는 사용자 결정 사항이다.
- 빈 패키지에 구현 코드·클래스·함수를 미리 만들지 마라. 이유: 어느 마일스톤에서 어떤 시그니처가
  필요한지는 그 step에서 정한다. 추측한 인터페이스는 이후 step이 지워야 하는 부채가 된다.
- `.env` 파일을 열거나 출력하지 마라. 이 step에서는 환경변수를 읽을 필요가 전혀 없다.
- `data/`, `docs/results/`, `logs/traces/`, `infra/` 디렉토리를 만들지 마라. 이유: 이 step의 범위가 아니다.
- `.gitignore`의 기존 항목을 지우지 마라. `uv.lock`을 gitignore에 넣지 마라.
- 기존 테스트를 깨뜨리지 마라.
