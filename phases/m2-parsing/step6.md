# Step 6: article-score

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md`, `docs/EVAL.md` — 원칙(결정론 우선), 파싱 지표, 결과 보고
- `docs/ADR.md` — ADR-012(결정론 채점), ADR-022(법제처 조문과 원본의 편집 차이 → M2에서 확인)
- `data/manifest/parse_targets.json` — `law_rule_id`, `law_serial`, `source_sha256`, `pdf_path`
- `src/kodocs/ingest/manifest.py` — `latest_records`, `sha256_file`
- step 1: `src/kodocs/parsing/schema.py`
- step 2: `src/kodocs/parsing/run.py`, `data/manifest/parse_runs.jsonl` 형식(`latest_records(key=(parser, input_sha256))`)
- step 5: `src/kodocs/parsing/articles.py` (`split_document`, `split_text`, `law_to_lines`)

## 배경

파서별 파싱 결과를 분할기에 넣어 조문 목록을 만들고, 해당 버전의 법제처 조문(`data/interim/law/{law_rule_id}/{law_serial}.json`)과 **조 번호로 짝지어** 비교한다. LLM을 쓰지 않는다.
평가 코드는 `src/kodocs/eval/`에 둔다(최상위 `eval/`이 아니다. ARCHITECTURE 반영은 step 8).
또 **분할기 상한**(법제처 조문을 이어 붙인 텍스트에 분할기를 돌렸을 때 법제처 조문이 복원되는 정도)을 함께 잰다. 파서 간 점수 차이가 분할기가 아니라 파서 때문임을 보이기 위해서다.

## 작업

TDD: `tests/eval/test_parsing_score.py`, `tests/eval/test_text_normalize.py`를 먼저 작성한다. 입력은 테스트 안에서 만든 가짜 `ParsedDocument`·법제처 JSON·`parse_targets.json`이다.

### 1. `src/kodocs/eval/__init__.py`, `src/kodocs/eval/text_normalize.py`

정규화 규칙은 **이 모듈에만** 두고 docstring에 표로 적는다(step 8이 EVAL.md에 옮긴다).

```python
EDITORIAL_PATTERNS: tuple[re.Pattern, ...]
    # <개정 …>, <신설 …>, <삭제 …>, <종전 …>, [본조신설 …], [전문개정 …], [종전 …], [제목개정 …], [시행일 …]
    # (각괄호·대괄호 안이 날짜·쉼표·공백·"제N조" 등으로 이루어진 편집 주석. 괄호 짝이 없으면 지우지 않는다)
def raw_compare_form(s: str) -> str: ...        # NFC + 앞뒤 공백 제거만 ("원문 비교")
def normalize(s: str) -> str: ...               # 아래 N1~N5 순서대로 ("정규화 비교")
def strip_editorial(s: str) -> tuple[str, int]: ...   # 주석 제거 결과와 제거 개수
```

정규화 규칙(순서 고정):
- N1. 유니코드 NFC. (NFKC는 쓰지 않는다: `①`→`1`처럼 항 번호가 바뀐다)
- N2. 편집 주석 제거(`EDITORIAL_PATTERNS`).
- N3. 따옴표·가운뎃점 변형 통일: `“”„‟` → `"`, `‘’‚‛` → `'`, `ㆍ`·`・`·`･`·`‧` → `·`.
- N4. 모든 공백 문자(스페이스, 탭, 줄바꿈, NBSP, 전각 공백) 제거. (법제처는 항·호 사이 줄바꿈이 없고 파서는 줄바꿈이 있어, 공백 위치를 비교하면 파서 차이가 아니라 줄바꿈 차이를 잰다. 구두점 앞 공백 차이도 이 규칙으로 사라진다)
- N5. 그 외 문자는 바꾸지 않는다.

### 2. `src/kodocs/eval/parsing_score.py`

```python
@dataclass(frozen=True)
class DocScore:
    parser: str
    target_key: str
    input_sha256: str
    status: str                 # parse_runs의 최신 상태: ok | empty | failed | missing
    law_articles: int           # 법제처 본문 조 수 (장·절 제목 제외)
    parsed_articles: int
    matched: int                # (number, branch) 키가 양쪽에 있는 조 수
    key_precision: float
    key_recall: float
    order_lcs_ratio: float      # 법제처 키 순서와 파싱 키 순서의 LCS 길이 / law_articles
    structure_exact: bool       # 키 순서열이 정확히 같음
    addenda_law: int
    addenda_parsed: int
    text_sim_raw_mean: float    # 짝지어진 조의 원문 비교 문자 유사도 평균
    text_sim_norm_mean: float   # 정규화 비교 문자 유사도 평균
    text_exact_raw_rate: float  # 짝지어진 조 중 원문 비교 완전 일치 비율
    text_exact_norm_rate: float
    editorial_law_only: int     # 짝지어진 조 중 법제처 쪽에만 편집 주석이 있는 조 수
    editorial_parsed_only: int  # 파싱 쪽에만 있는 조 수
    editorial_both: int

def score_document(parsed: ParsedDocument | None, law: Mapping, *, status: str, target_key: str, parser: str) -> DocScore: ...
def splitter_ceiling(law: Mapping, *, target_key: str) -> DocScore: ...   # law_to_lines → split_text → 같은 채점, parser="splitter-ceiling"
def aggregate(scores: Sequence[DocScore]) -> dict: ...
def run(root: Path, *, parsers: Sequence[str] | None = None, out: TextIO = sys.stdout) -> dict: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.eval.parsing_score [--parser ...]
```

규칙:
- **법제처 본문 조**: `law["articles"]`를 `law_to_lines` → `split_text`로 나눈 결과가 아니라, 원래 `articles` 항목 중 조 머리 정규식(step 5 모듈 상수)으로 시작하는 항목을 정답으로 쓴다. 장·절 제목 항목은 제외한다. 정답 추출과 분할기를 섞지 않기 위해서다.
- **짝짓기 키**: `(number, branch)`. 한쪽에 같은 키가 여러 개면 처음 것만 짝짓고 나머지는 짝 없음으로 센다.
- **문자 유사도**: `difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()`. 원문 비교는 `raw_compare_form`, 정규화 비교는 `normalize`를 양쪽에 적용한 뒤 계산한다. 비교 대상은 조 머리부터 조 끝까지 전체 텍스트다.
- **편집 차이 집계(ADR-022)**: 짝지어진 조마다 `strip_editorial`의 제거 개수가 법제처 쪽에만 > 0, 파싱 쪽에만 > 0, 양쪽 모두 > 0인지 센다. 파서 차이가 아니라 원본과 법제처 사이 편집 차이의 크기를 보이는 값이다.
- **무엇을 세는지(FP-043)** — docstring과 결과에 명시:
  - 문서 수 분모 = `parse_targets.json`의 대상 수(115). 파싱 결과가 없거나 `failed`·`empty`인 대상은 `status`를 남기고 조 지표를 0으로 넣는다.
  - 집계는 두 가지를 모두 낸다: (a) 전체 대상 기준(실패 0점 포함), (b) `status=ok`인 대상만. 각각 문서 수를 함께 적는다.
  - 텍스트 지표의 평균은 **짝지어진 조 단위 평균**(micro)과 **문서 단위 평균**(macro)을 모두 낸다.
  - 역할별(`current`/`history`) 집계도 낸다.
- **분할기 상한**: 대상마다 `splitter_ceiling`을 계산해 파서와 같은 표에 `splitter-ceiling` 행으로 낸다. 기대값은 구조 완전 일치 100%에 가깝다. 100%가 아닌 대상은 target_key와 원인(짝 없는 키 목록)을 출력한다.
- **입력 읽기**: 파싱 결과는 `parse_runs.jsonl`의 `latest_records`로 상태를 보고(FP-032), 출력 파일은 `schema.output_path`로 찾는다. 법제처 파일이 없으면 그 대상을 `status="missing_law"`로 남기고 계속한다.
- 한 파서의 모든 대상 결과가 없으면(0건) 그 파서는 "실행 안 됨"으로 출력하고 점수 0으로 처리하지 않는다. 모든 파서가 0건이면 실패로 멈추고 먼저 실행할 명령을 안내한다(FP-031).

출력:
- `data/manifest/parse_scores.json`(커밋 대상, **원문 없음**): `generated_at`, `git_commit`(가능하면 `git rev-parse HEAD`), `parse_targets_sha256`, `normalization_rules`(N1~N5 설명 문자열), 파서별 `aggregate`, 대상별 `DocScore`(숫자·키·상태만).
- `data/interim/parse_diffs/{parser}/{input_sha256}.json`(커밋 안 함): 짝 없는 키, 유사도가 낮은 조의 양쪽 텍스트. 사람이 분할 결과를 눈으로 확인하는 데 쓴다.
- 표준 출력: 파서별 요약 표(마크다운) — 파서, 문서 수(ok/전체), 구조 완전 일치율, 키 F1 평균, 순서 LCS 평균, 정규화 유사도(micro), 정규화 완전 일치율(micro), 원문 유사도(micro).

### 3. Make 타깃

- `parse-score: uv run python -m kodocs.eval.parsing_score`. `.PHONY`에 추가.
- `AGENTS.md` 명령어 절: `make parse-score     # 파서별 파싱 결과를 조문 분할 후 법제처 조문과 대조(분할기 상한 포함) — 네트워크 없음, 사용자 실행`

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.eval.parsing_score --help
uv run python -c "from kodocs.eval.text_normalize import normalize; assert normalize('제1조 (목적)\n“이 규정”은 <개정 2021. 3. 16.>') == '제1조(목적)\"이규정\"은'; print('ok')"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - `parse_scores.json`에 원문 텍스트가 들어가지 않는가(테스트로 확인)?
   - 정규화가 `text_normalize.py` 한 곳에만 있는가? 원문 비교와 정규화 비교가 따로 보고되는가?
   - 분모(대상 115, ok만, 짝지어진 조)가 결과에 함께 기록되는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make parse-score`를 실데이터로 실행하지 마라. 이유: `data/manifest/parse_scores.json`이 생겨 작업 트리가 바뀐다(FP-042). 실행은 사용자가 한다.
- LLM으로 채점하지 마라. 이유: ADR-012, 결정론으로 잴 수 있다.
- 정규화 결과만 보고하지 마라. 원문 비교를 항상 함께 낸다. 이유: FP-020, 정규화가 파서 오류를 가릴 수 있다.
- 법제처 정답 텍스트를 수정하거나 정규화된 값으로 덮어쓰지 마라. 이유: FP-020(정답 텍스트는 정제 전후 동일). 비교 시점에만 변환한다.
- `parse_scores.json`이나 표준 출력에 조문 원문을 넣지 마라. 이유: 원문 재배포 금지(ADR-002). 원문 diff는 `data/interim/`에만.
- 실패·미실행 대상을 분모에서 조용히 빼지 마라. 이유: FP-043, 점수 부풀림. 두 분모를 모두 보고한다.
- `data/golden/`을 만들거나 수정하지 마라.
- 기존 테스트를 깨뜨리지 마라.
