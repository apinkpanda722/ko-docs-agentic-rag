# Step 5: article-splitter

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `docs/ADR.md` — ADR-007(조문 분할은 파서와 독립, `제N조` 정규식), ADR-022(법제처 조문이 정답, 편집 차이 가능성)
- `docs/EVAL.md` — 조항 구조 보존 지표
- `docs/results/2026-09-26-m1-data-collection.md` — 6절(별표 확인 방법)
- `src/kodocs/ingest/law_api.py` — 정제 JSON 구조(`articles`: 문자열 목록, `addenda`: `{date, text}` 목록, `annexes`: `{number, branch, kind, title, …}` 목록)
- step 1 산출물: `src/kodocs/parsing/schema.py` (`ParsedDocument`, `Block`, `BlockKind`)

## 배경 — 실측한 입력 형태

- 법제처 `articles`의 각 항목은 문자열 하나다. 조 번호 필드가 따로 없다. 항·호 사이에 줄바꿈이 없다.
  `제1장 총칙` 같은 **장·절 제목이 조와 같은 층위의 항목으로 섞여 있다**(267개 버전에서 1,065개).
  `<개정 2021. 3. 16.>`, `<신설 …>`, `[본조신설 …]`, `[전문개정 …]` 같은 편집 주석이 본문에 붙어 있다.
- 부칙은 `addenda`로 분리되어 있고, 부칙 안에서 `제1조(시행일)`처럼 조 번호가 다시 1부터 시작한다.
- ALIO 원본(→ 변환 PDF)의 별표·별지 표기는 여러 괄호로 나온다: `[별표 1]`, `【 별표 1 】`, `<별표 1>`, `[별지 제1호서식]`, `[별지 1]`. 원본에도 `<신설 2025.1.9.>` 같은 주석이 있다.
- 본문에는 "제7조제3항에 따라", "별표 1과 같다" 같은 **참조**가 흔하고, pypdf 줄바꿈 때문에 참조가 줄 첫머리에 올 수 있다.

## 작업

TDD: `tests/parsing/test_articles.py`를 먼저 작성한다. 입력은 테스트 안에서 만든 `ParsedDocument`와 가짜 법제처 JSON이다(실제 `data/interim/` 파일을 쓰지 마라).

### 1. `src/kodocs/parsing/articles.py`

```python
@dataclass(frozen=True)
class Article:
    number: int            # 제N조의 N
    branch: int            # 제N조의M의 M, 없으면 0
    title: str | None      # 괄호 안 제목, 삭제 조는 None
    deleted: bool          # "제N조 삭제" 형태
    text: str              # 조 머리부터 다음 경계 직전까지. 블록/줄 사이는 "\n"으로 잇는다. 이 외의 변형은 하지 않는다.
    first_page: int | None

@dataclass(frozen=True)
class AddendumBlock:
    heading: str           # "부칙 <제9999호, 2021.03.16.>" 같은 머리 줄
    articles: tuple[Article, ...]   # 부칙 안 조문(번호가 다시 시작). 조가 없으면 빈 튜플
    text: str

@dataclass(frozen=True)
class AnnexMarker:
    kind: str              # "별표" | "별지" | "서식" | "부록"
    label: str             # 원문 표기 그대로, 예: "【 별표 1 】"
    number: str | None     # "1", "1의2" 등
    page: int | None
    block_index: int

@dataclass(frozen=True)
class SplitResult:
    articles: tuple[Article, ...]        # 본문 조문만
    headings: tuple[str, ...]            # 장·절 제목 (조문 텍스트에서 제외됨)
    addenda: tuple[AddendumBlock, ...]
    annexes: tuple[AnnexMarker, ...]
    preamble: str                        # 첫 조 전 텍스트(규정명·제정 이력 등)
    dropped_noise: int                   # 제거한 잡음 줄 수

def split_document(doc: ParsedDocument) -> SplitResult: ...
def split_text(lines: Sequence[tuple[int | None, str]]) -> SplitResult: ...   # (page, line) 목록. split_document가 이 함수를 부른다
def law_to_lines(law: Mapping) -> list[tuple[None, str]]: ...
    # 법제처 정제 JSON → 분할기 입력 줄: articles 항목(장 제목 포함)을 순서대로 한 줄씩,
    # 그다음 addenda마다 text를 한 줄씩(부칙 머리 "부칙 <…>"로 시작함), 그다음 annexes마다 "[{kind} {int(number)}] {title}" 한 줄씩(법제처 `number`는 "0001"처럼 0이 채워져 있다. `branch`가 "00"이 아니면 "{int(number)}의{int(branch)}")
```

**경계 규칙 (파서 무관, 결정론)** — 정규식은 모듈 상수로 두고 테스트로 고정한다.

1. **조 머리**: 줄 첫머리(앞 공백 허용)에서
   `제\s*(\d+)\s*조(?:\s*의\s*(\d+))?` 뒤에 다음 중 하나가 와야 한다.
   - 괄호 제목: `\s*[(（]\s*([^)）]{1,40})\s*[)）]` — 괄호 앞 공백 허용(`제1조 (목적)`)
   - 【】 제목: `\s*【\s*([^】]{1,40})\s*】` (`제1조【목적】`)
   - 삭제: `\s*삭\s*제` (`제5조 삭제 <2021. 3. 16.>`) → `deleted=True`, `title=None`
   이 조건 때문에 "제7조제3항에 따라…"처럼 조 번호 뒤에 제목이 없는 참조는 조 머리가 아니다. 이 사례를 테스트로 고정한다.
2. **장·절 제목**: 줄 전체가 `^\s*제\s*\d+\s*(장|절|관)(?!\s*의\s*\d)` 로 시작하고 40자 이하(`제1장 총칙`, `제1장총칙`, `제2절 운영` 모두 해당) → `headings`에 넣고 조문 텍스트에서 뺀다.
3. **부칙**: 줄 첫머리 `^\s*부\s*칙\b` → 새 `AddendumBlock` 시작. 이후 조 머리는 그 부칙 블록의 `articles`에 넣는다(본문 조와 섞지 않는다). 부칙이 여러 번 나오면 블록도 여러 개.
4. **별표·별지 경계**: 줄 첫머리에서 괄호 `[ ]`, `【 】`, `< >`, `〔 〕` 중 하나로 감싼 `별표|별지|서식|부록` 표기
   (`^\s*[\[【<〔]\s*(별\s*표|별\s*지|서\s*식|부\s*록)\s*(제?\s*\d+(?:\s*의\s*\d+)?\s*호?)?[^\]】>〕]*[\]】>〕]`) → `AnnexMarker`를 남기고,
   이후 텍스트는 어느 조문·부칙에도 붙이지 않는다(다음 조 머리나 부칙이 나오면 그쪽으로 다시 전환). 괄호 없이 문장 속에 나오는 "별표 1과 같다"는 경계가 아니다(테스트).
5. **잡음 제거**(`split_document`에서, 줄 단위):
   - 파서가 `kind`를 `header`·`footer`·`page_number`로 붙인 블록은 버린다.
   - 쪽 번호 패턴 줄을 버린다: `^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$`, `^\s*\d{1,4}\s*/\s*\d{1,4}\s*$`, `^\s*-\s*\d+\s*-\s*$`.
   - 쪽 정보가 있고 쪽 수가 3 이상이면, 각 쪽의 첫 2줄·마지막 2줄 중 **과반수 쪽에서 똑같이 반복되는 줄**을 머리말·꼬리말로 보고 버린다.
   - 버린 줄 수는 `dropped_noise`에 센다. 조 머리·부칙·별표 표기 줄은 잡음으로 버리지 않는다.
6. `kind="table"` 블록의 텍스트는 현재 구간(조문/부칙/별표)에 그대로 이어 붙인다.

**무엇을 세는지 (FP-043)** — 모듈 docstring과 테스트에 명시한다.
- 본문 조 수 = `articles` 길이. `제N조의M`은 별도 조로 센다. 삭제된 조(`deleted=True`)도 센다.
- 장·절 제목, 부칙 안의 조, 별표 뒤 텍스트는 본문 조 수에 넣지 않는다.
- 조 번호가 줄어들거나 중복돼도 버리지 않고 순서대로 남긴다(구조 오류는 채점에서 드러나야 한다).

### 2. 분할기 상한 테스트

`law_to_lines(law) → split_text`를 가짜 법제처 JSON(장 제목, `제N조의M`, 삭제 조, `<개정 …>` 주석, 부칙 2개와 부칙 안 `제1조`, 별표 2개 포함)에 돌려
본문 조의 (number, branch) 순서와 텍스트가 원래 `articles`(장 제목 제외)와 **정확히 같은지** 검사한다. 부칙 블록 수 = `addenda` 길이.
이 값은 step 6에서 실제 법제처 데이터 전체에 대해 "분할기 상한" 지표로 다시 잰다.

## Acceptance Criteria

```bash
make verify
uv run python -c "from kodocs.parsing.articles import split_text, law_to_lines; r = split_text([(1, '제1조 (목적) 이 규정은'), (1, '제7조제3항에 따라 한다.'), (1, '【 별표 1 】 수당표')]); assert len(r.articles) == 1 and len(r.annexes) == 1; print('ok')"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 분할기가 파서 이름을 보고 분기하지 않는가? (`kind` 라벨만 본다)
   - 조문 텍스트를 정규화하지 않는가?
   - 테스트가 괄호 앞 공백, 【】 제목, 삭제 조, 참조 오탐, 부칙 번호 재시작, 별표 괄호 변형 4종, 문장 속 "별표 1과", 반복 머리말, 쪽 번호를 다루는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 분할기 안에서 파서별 분기(`if doc.parser == ...`)를 두지 마라. 이유: ADR-007. 분할이 파서마다 다르면 파서 차이와 분할 차이가 섞인다.
- 조문 텍스트를 정규화(공백 제거, 주석 제거, 따옴표 통일)하지 마라. 줄을 `\n`으로 잇는 것 외에는 바꾸지 마라. 이유: FP-020. 정규화는 채점(step 6)에서 원문 비교와 분리해 한다.
- 조 번호가 이상하다고(역순·중복) 조를 버리거나 재정렬하지 마라. 이유: 구조 오류를 숨기면 조 구조 지표가 부풀려진다.
- 실제 `data/interim/law/`나 `data/interim/parsed/` 파일을 테스트 입력으로 쓰지 마라. 이유: 원문은 저장소에 없고(ADR-002), CI에서 재현되지 않는다.
- 기존 테스트를 깨뜨리지 마라.
