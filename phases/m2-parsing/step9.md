# Step 9: upstage-hwp-direct (보조 실험)

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md`, `docs/ADR.md` — ADR-008("업스테이지 HWP 직접 파싱은 공식 지원 확인 시 추가"), ADR-014, ADR-023(step 8에서 추가됨)
- `docs/EVAL.md` — 파싱 지표(step 8에서 갱신됨)
- `data/manifest/parse_targets.json` — `source_path`, `source_sha256`, `source_kind`(`hwp5`|`hwpx`), `page_count`(변환 PDF 기준), `role`
- step 0: `src/kodocs/costs.py`
- step 1: `src/kodocs/parsing/schema.py` (`input_kind`, `output_path`)
- step 2: `src/kodocs/parsing/run.py` (`ParseInput`, `load_inputs`, `ADAPTERS`, 실행 기록)
- step 4: `src/kodocs/parsing/upstage_adapter.py` (`REQUEST_OPTIONS`, `cache_path(root, input_kind, input_sha256)`, `UpstageClient`, `plan`, 확인 입력)
- step 6·7: `src/kodocs/eval/parsing_score.py`, `src/kodocs/eval/table_score.py`, `data/manifest/table_golden.json` 형식
- step 8: `src/kodocs/eval/parsing_report.py`, `src/kodocs/eval/templates/m2_parsing.md`

## 배경

3종 파서 비교는 변환 PDF 입력으로 고정한다(파서만 바뀌는 비교). 이 step은 **업스테이지 한정으로 입력만 바꾸는** 보조 비교(변환 PDF vs HWP/HWPX 원본)다.
일정이 밀리면 이 step은 빼도 된다(PRD의 범위 축소 순서상 업스테이지 비교가 먼저 빠진다).

공식 문서로 확인한 사양(2026-09-26, console.upstage.ai/docs):
- Document Parse 지원 형식에 HWP, HWPX가 있다(FAQ "What file formats are supported", 모델 비교 표).
- 요청·응답 형식, 모드, 파일당 100쪽 한도는 PDF와 같다(step 4 사양).
- **HWP·HWPX의 과금 쪽수 계산 방식은 공식 문서에 없다.** 응답 `usage.pages`가 실제 과금 쪽수다. 그래서 호출 전 예상 쪽수는 변환 PDF 쪽수 × 1.5(보수적 여유)로 잡고, 실제 기록은 `usage.pages`로 한다. 이 가정을 settings·결과 문서에 적는다.

## 작업

TDD: `tests/parsing/test_upstage_direct.py`, `tests/eval/test_direct_compare.py`를 먼저 작성한다. HTTP는 `httpx.MockTransport`로 mock한다.

### 1. 입력 선정 — `run.py` 확장

```python
def load_direct_inputs(root: Path, *, max_pages: int = 300, hwp5_count: int = 20) -> list[ParseInput]: ...
```

결정론 선정 규칙(무엇을 세는지 명시, FP-043):
1. `source_kind == "hwpx"`인 대상 전부(현재 10개). `table_golden.json`이 있으면 표 정답이 있는 대상을 먼저 둔다.
2. 그다음 `source_kind == "hwp5"`이고 `role == "current"`인 대상을 `rule_no` 오름차순으로 훑으며, **누적 쪽수 + 이 대상 쪽수 ≤ `max_pages`**인 것만 넣는다(넘는 대상은 건너뛰고 다음을 본다). `hwp5_count`개를 채우면 멈춘다.
3. 쪽수는 **변환 PDF의 `page_count`**로 센다(원본의 실제 과금 쪽수는 호출 전에 알 수 없다). `page_count`가 없는 대상은 넣지 않는다.
4. 선정 결과(대상 수, HWPX 수, HWP5 수, 누적 PDF 쪽수)를 출력한다. 참고: HWPX 10개만 PDF 기준 271쪽이라 기본값(300쪽)이면 HWP5는 짧은 문서 몇 개만 들어간다. 사용자는 `--max-pages`로 조정할 수 있다.

- `ParseInput`: `input_path = source_path`, `input_sha256 = 원본 파일을 읽어 계산한 sha256`(manifest `source_sha256`과 다르면 `failed`), `input_kind = source_kind`, `page_count = PDF page_count`.
- HWP/HWPX 형식은 manifest의 `source_kind`를 쓴다(확장자로 판정하지 않는다, FP-010).

### 2. 어댑터 — `upstage_adapter.py` 확장

- `PARSER_NAME_DIRECT = "upstage-direct"`. 요청 옵션 `REQUEST_OPTIONS`는 **step 4와 완전히 같다**(입력만 바뀐다). `settings`에 `{"input": "original", "request_options": ..., "estimate_page_factor": 1.5}`.
- 캐시 키 = `(input_kind, 원본 sha256)` → `data/raw/upstage/{hwp5|hwpx}/{sha256}.json`(step 4의 `cache_path` 그대로). PDF 캐시와 섞이지 않는다. 캐시가 있으면 호출하지 않는다(FP-033).
- 예상 비용·상한 검사: `estimate_usd(SERVICE, ceil(page_count * 1.5))`. `ceil(page_count * 1.5) > 100`이어도 호출은 하되, 응답 `usage.pages >= 100`이면 `possibly_truncated=True`로 settings와 실행 기록에 남기고 경고를 출력한다(앞 100쪽만 처리됐을 수 있다).
- 업로드 파일 이름은 원본 확장자를 유지한다(`.hwp`/`.hwpx`). 원본 파일명의 한글은 그대로 두되, 로그·진행 줄에는 sha256 앞 12자만 쓴다.
- 원장 기록·429 처리·순차 호출·키 비노출·dry-run·확인 입력은 step 4 구현을 재사용한다. 새로 구현하지 마라.
- 파싱 결과: `data/interim/parsed/upstage-direct/{원본 sha256}.json`, 실행 기록은 같은 `parse_runs.jsonl`.

### 3. CLI와 make 타깃

- `python -m kodocs.parsing.run --parser upstage-direct [--dry-run] [--yes] [--max-pages N] [--hwp5-count N]`
- `Makefile`: `parse-upstage-direct-dry: ... --parser upstage-direct --dry-run`, `parse-upstage-direct: ... --parser upstage-direct`. `.PHONY`에 추가.
- `AGENTS.md` 명령어 절: `make parse-upstage-direct-dry  # 보조 실험: 업스테이지 HWP/HWPX 직접 입력 예상 비용 — 네트워크 없음`,
  `make parse-upstage-direct      # 보조 실험: 업스테이지 HWP/HWPX 직접 입력 파싱 — 실제 API 호출·과금, 사용자 실행`

### 4. 채점 — 변환 경유 vs 직접

- `parsing_score.py`: `upstage-direct` 결과는 `input_sha256 = source_sha256`으로 대상과 잇는다(다른 파서는 PDF sha256). 채점 로직(분할·정규화·짝짓기)은 **step 6과 같은 함수**를 쓴다.
- `table_score.py`: `upstage-direct`는 golden의 `source_sha256`으로 문서를 찾는다. 채점 함수는 step 7과 같다.
- `src/kodocs/eval/direct_compare.py`: **두 입력 모두 결과가 있는 대상만** 짝지어(짝 비교) `upstage`(PDF) vs `upstage-direct`(원본)의 구조 완전 일치율, 키 F1, 정규화·원문 유사도, 표 셀 일치율, 과금 쪽수 합(원장 `ref`별), 쪽당 시간을 비교한다. 짝지은 대상 수를 함께 적는다.
- `parsing_report.py`: 결과 문서의 생성 표에 "변환 경유 vs 직접" 표를 추가한다. 결과가 없으면 `(실행 후 기록)`.
- 과금 쪽수 비교 표에 "HWP 과금 쪽수 = 응답 usage.pages, PDF 쪽수와 다를 수 있음"을 적는다.

### 5. 문서

- `docs/ADR.md` ADR-008 갱신: 결정에 "업스테이지 HWP·HWPX 직접 입력은 공식 지원을 확인했다(2026-09-26). 3종 비교는 PDF 입력으로 고정하고, 직접 입력은 업스테이지 한정 보조 실험(입력만 바꿈, HWPX 전부 + HWP5 현행 일부, PDF 기준 300쪽 이내)으로 측정한다." 트레이드오프에 "HWP 과금 쪽수 계산 방식이 공개되지 않아 예상 비용은 PDF 쪽수 × 1.5로 잡는다" 추가.
- `docs/EVAL.md` 파싱 절에 보조 실험 한 줄(짝 비교, 고정 조건: 같은 요청 옵션·같은 분할기·같은 채점).
- `src/kodocs/eval/templates/m2_parsing.md`의 7절과 권장 실행 순서 9)에 명령(`make parse-upstage-direct-dry` → `make parse-upstage-direct` → `make parse-score` → `make table-score` → `make parse-report`)을 적는다.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.parsing.run --help | grep -n -- "--max-pages"
grep -n "parse-upstage-direct" Makefile AGENTS.md
grep -n "직접 입력" docs/ADR.md docs/EVAL.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 테스트: 선정 규칙(HWPX 우선, 쪽수 상한 건너뛰기, `hwp5_count` 상한, `page_count` 없음 제외), PDF 캐시와 원본 캐시 분리, 캐시 있으면 호출 0회, 요청 옵션이 PDF 경로와 동일, 1.5배 예상 비용으로 상한 검사, `usage.pages >= 100` 경고, 짝 비교가 양쪽 결과가 있는 대상만 쓰는지.
   - 채점·분할·정규화 코드를 복제하지 않았는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 업스테이지 API를 실제로 호출하거나 직접 입력 명령을 실데이터로 실행하지 마라. 이유: ADR-014, FP-040.
- 요청 옵션(`mode` 등)을 PDF 경로와 다르게 두지 마라. 이유: 입력 하나만 바꾸는 비교다(한 번에 한 변수).
- HWP 과금 쪽수 계산 방식을 추측해 단가·쪽수를 코드에 박지 마라. 이유: 공식 문서에 없다. 예상은 PDF 쪽수 × 1.5(문서화한 가정), 실제는 `usage.pages`.
- 원본 캐시 키를 PDF sha256이나 파일명으로 잡지 마라. 이유: FP-033, FP-014(확장자만 다른 입력의 출력 덮어쓰기).
- 3종 파서 비교 결과표에 `upstage-direct`를 같은 층위의 네 번째 파서로 섞지 마라. 이유: 입력이 달라 파서 비교 조건이 깨진다. 별도 표로만 보고한다.
- `data/golden/`을 만들거나 수정하지 마라.
- 기존 테스트를 깨뜨리지 마라.
