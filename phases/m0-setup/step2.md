# Step 2: config-loader

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 특히 CRITICAL 규칙 중 "외부 LLM 호출은 `src/kodocs/llm/` 래퍼를 통해서만",
  "실제 API를 호출하는 스크립트는 누적 비용을 기록하고 설정한 월 상한을 넘기 전에 멈춘다",
  "`make verify`는 API 키 없이 통과해야 한다", "`.env` 파일을 열거나 출력하지 않는다"
- `docs/ADR.md` — ADR-004(모델 역할 분리: 생성 Anthropic / 답변 OpenAI / 채점 Gemini),
  ADR-014(실제 API 호출은 사용자가 직접 실행)
- `docs/ARCHITECTURE.md` — "비밀 값: API 키는 `.env`에서만 읽는다"
- `docs/PRD.md` — 일정과 예산 절(월 3~5만 원 상한)
- `.env.example` — 현재 정의된 변수
- 이전 step에서 만들어진 파일: `pyproject.toml`, `Makefile`, `src/kodocs/` 패키지 골격,
  `tests/test_architecture.py`, `tests/test_infra_compose.py`, `infra/`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

step 0에서 uv 골격과 `make verify`, step 1에서 OpenSearch Docker 구성이 만들어졌다.
`.env.example`에는 API 키 4개가 이미 정의되어 있지만, 이를 읽는 코드가 없어 문서로만 존재한다.

이 step은 설정을 읽는 단일 진입점을 만든다. 범위는 **API 키 4개와 월 비용 상한까지다.**

## 작업

### 1. 테스트를 먼저 작성한다 (TDD)

`tests/test_config.py`를 먼저 만든다. 검증할 동작:

- **키가 하나도 없어도 `load_settings()`가 성공한다.** 예외를 던지지 않는다.
  이것이 이 모듈의 가장 중요한 성질이다. `make verify`는 API 키 없이 통과해야 하고(AGENTS.md CRITICAL),
  설정을 로드하는 것만으로 실패하면 모든 테스트가 개발자의 `.env` 존재 여부에 묶인다.
- 키가 주어지면 그 값을 읽는다.
- `require_key("OPENAI_API_KEY")`는 키가 있으면 값을 돌려주고, 없으면 에러를 던진다.
  에러 메시지에 **어느 환경변수가 없는지**와 `.env.example`을 참고하라는 안내가 들어간다.
- `require_key`에 정의되지 않은 이름을 넘기면 조용히 `None`을 돌려주지 않고 에러를 낸다
  (오타로 인한 무음 실패 방지).
- `MONTHLY_BUDGET_USD`가 미설정이거나 빈 문자열이면 기본값 `35.0`이다.
- `MONTHLY_BUDGET_USD`가 숫자로 파싱되지 않거나 음수면 명확한 에러를 낸다.
  이유: 오타 하나가 상한을 무력화하면 과금 사고가 된다. 조용히 기본값으로 떨어지면 안 된다.
- `Settings`의 `repr()`·`str()`에 **키 값이 드러나지 않는다.** 이유: 트레이스와 에러 로그에
  설정 객체가 찍히면 키가 로그에 남는다.
- `.env.example`에 선언된 변수 이름 집합과 `config`가 읽는 변수 이름 집합이 일치한다.
  이유: 변수 이름이 어긋나면 사용자가 키를 넣었는데도 코드가 못 읽는다.
  `.env.example`을 파싱해 `KEY=` 형태의 변수 이름만 뽑아 비교한다.

테스트는 **`monkeypatch`한 환경변수나 명시적으로 넘긴 매핑만 쓴다.** 실제 `.env` 파일에 의존하는
테스트를 만들지 마라. 개발자의 `.env`에 진짜 키가 있으면 테스트 결과가 기기마다 달라진다.

### 2. `src/kodocs/config.py`

시그니처 수준 지시:

```python
KEY_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",   # 합성 문서·질의 생성 (ADR-004)
    "OPENAI_API_KEY",      # 시스템 답변
    "GEMINI_API_KEY",      # 답변 채점
    "UPSTAGE_API_KEY",     # 파서 비교
)

DEFAULT_MONTHLY_BUDGET_USD = 35.0


class MissingAPIKeyError(RuntimeError):
    """필요한 시점에 API 키가 없을 때. 어떤 환경변수가 없는지 메시지에 담는다."""


@dataclass(frozen=True)
class Settings:
    """값이 있는 API 키와 월 비용 상한. 키 값은 repr에 노출하지 않는다."""

    api_keys: Mapping[str, str]        # 값이 있는 키만 담는다
    monthly_budget_usd: float

    def require_key(self, name: str) -> str:
        """키를 요구하는 시점에 호출한다. 없으면 MissingAPIKeyError."""


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """설정을 읽는다. 키가 없어도 실패하지 않는다.

    env가 주어지면 그 매핑만 읽고 .env·os.environ을 건드리지 않는다(테스트용).
    env가 None이면 python-dotenv로 .env를 로드한 뒤 os.environ을 읽는다.
    """
```

핵심 규칙:
- **`load_settings()`는 키가 없어도 절대 실패하지 않는다.** 키는 선택값이다.
  실패는 키가 실제로 필요한 시점에 `require_key()`가 낸다.
- `env` 인자가 주어지면 `.env`와 `os.environ`을 읽지 않는다. 테스트가 기기 환경에서 격리된다.
- 빈 문자열은 "없음"으로 취급한다. `.env.example`은 값이 비어 있는 상태로 배포되므로,
  `ANTHROPIC_API_KEY=`만 있는 환경에서 키가 있다고 판단하면 안 된다.
- 키 값을 로그·예외 메시지·`repr`에 넣지 마라. 에러 메시지에는 환경변수 **이름**만 쓴다.

### 3. `.env.example` 갱신

- `MONTHLY_BUDGET_USD=35` 를 추가하고, 무엇을 제한하는 값인지 주석으로 적는다
  (API 월 누적 비용 상한. 이 값을 넘기기 전에 생성·평가 스크립트가 멈춘다).
- 현재 `.env.example`은 값 뒤에 인라인 주석이 붙어 있다(`ANTHROPIC_API_KEY=   # 합성 문서·질의 생성`).
  **주석을 각 변수 위의 줄로 옮겨라.** 이유: 인라인 주석 처리는 파서마다 다르고, 사용자가 이 파일을
  복사해 키를 채울 때 값 뒤에 `# ...`가 남으면 키가 조용히 망가진다.
- 변수 이름과 개수는 바꾸지 마라(키 4개 + 상한 1개).

## Acceptance Criteria

```bash
make verify                                    # API 키 없이 통과해야 한다
uv run python -c "from kodocs.config import load_settings; s = load_settings({}); print(s.monthly_budget_usd, s.api_keys)"
# → 35.0 {} : 키가 하나도 없어도 성공하고 기본 상한을 돌려준다
uv run python -c "from kodocs.config import load_settings; print(repr(load_settings({'OPENAI_API_KEY': 'sk-should-not-appear'})))"
# → 출력에 'sk-should-not-appear' 가 없어야 한다
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. 두 번째·세 번째 커맨드의 출력을 눈으로 확인한다.
2. 아키텍처 체크리스트를 확인한다:
   - `src/kodocs/config.py` 위치가 `docs/ARCHITECTURE.md` 구조와 맞는가?
   - ADR 기술 스택을 벗어나지 않았는가? (추가 의존성 없음 — `python-dotenv`는 step 0에서 이미 넣었다)
   - AGENTS.md CRITICAL 규칙을 위반하지 않았는가? (키 없이 `make verify` 통과, `.env` 열지 않음)
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- **키가 없을 때 `load_settings()`가 예외를 던지게 만들지 마라.** 이유: AGENTS.md CRITICAL —
  `make verify`는 API 키 없이 통과해야 한다. 키 부재는 `require_key()` 시점의 문제다.
- **모델명·temperature를 `config.py`나 `.env.example`에 넣지 마라.** 이유: 어떤 모델을 쓸지는
  그 모델을 쓰는 마일스톤에서 확정한다(M3 생성, M6 답변, M7 채점). 지금 넣으면 검증되지 않은
  모델 ID가 코드에 박히고, 이후 step이 그 값을 사실로 믿는다.
- OpenSearch 주소·SQLite 경로·인덱스 이름·청킹 파라미터 같은 다른 설정을 추가하지 마라.
  이유: 이 step의 범위는 키 4개와 월 상한이다. 검색 설정은 M5의 측정 대상이다.
- 누적 비용 기록·과금 차단 로직을 만들지 마라. 이유: 비용 기록은 `src/kodocs/llm/` 래퍼와 함께
  M3에서 만든다. 여기서는 상한 **값을 읽는 것**까지다.
- `src/kodocs/llm/`에 코드를 추가하지 마라. 빈 패키지로 남겨 둔다.
- `.env` 파일을 열거나, 읽거나, 출력하거나, 내용을 테스트에 쓰지 마라. 실제 API를 호출하지 마라.
- 키 값을 예외 메시지·로그·`repr`에 넣지 마라.
- `Makefile`의 `verify` 타깃과 `scripts/` 아래 파일을 수정하지 마라.
- `.env.example`의 변수 이름을 바꾸거나 변수를 지우지 마라.
- 기존 테스트를 깨뜨리지 마라. `tests/test_architecture.py`, `tests/test_infra_compose.py`,
  `scripts/test_execute.py`가 `make verify`에서 계속 통과해야 한다.
