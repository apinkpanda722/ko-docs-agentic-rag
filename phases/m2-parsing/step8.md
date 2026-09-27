# Step 8: report-docs

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md`, `docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/ADR.md`, `docs/EVAL.md` — 이 step에서 모두 수정한다
- `docs/results/2026-09-26-m1-data-collection.md` — 결과 문서 형식(측정 정보 표: 실행일·커밋·도구 버전·설정 파일 sha256·고정 조건)
- step 0: `src/kodocs/costs.py` (`data/manifest/costs.jsonl`, `TAX_RATE`)
- step 2~4: `src/kodocs/parsing/run.py`, `pypdf_adapter.py`, `docling_adapter.py`, `upstage_adapter.py`, `data/manifest/parse_runs.jsonl` 형식
- step 6: `src/kodocs/eval/parsing_score.py`, `src/kodocs/eval/text_normalize.py`(정규화 규칙 N1~N5), `data/manifest/parse_scores.json` 형식
- step 7: `src/kodocs/eval/table_drafts.py`, `table_golden.py`, `table_score.py`, `data/manifest/table_golden.json`·`table_scores.json` 형식
- `Makefile`

## 배경

결과 문서는 사용자가 파싱·채점을 실행한 뒤에 수치가 생긴다. 이 step은 (1) manifest에서 표를 만들어 결과 문서에 넣는 생성기와 문서 틀, (2) 설계 문서 갱신을 한다.
**수치를 지어내지 않는다.** 실행 전에는 모든 수치 칸이 `(실행 후 기록)`이다.

## 작업

TDD: `tests/eval/test_parsing_report.py`를 먼저 작성한다. 입력은 `tmp_path`의 가짜 manifest들이다.

### 1. `src/kodocs/eval/parsing_report.py`와 문서 틀

```python
TEMPLATE_PATH = Path(__file__).parent / "templates" / "m2_parsing.md"
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"

def render_tables(root: Path) -> str: ...        # manifest만 읽어 마크다운 표 문자열 생성
def write_report(root: Path, *, run_date: str, out_path: Path | None = None) -> Path: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.eval.parsing_report [--date YYYY-MM-DD]
```

- 출력 파일 기본값: `docs/results/{run_date}-m2-parsing.md`(`run_date` 기본값은 오늘, Asia/Seoul).
  파일이 없으면 `TEMPLATE_PATH`를 복사해 만든다. 파일이 있으면 **`BEGIN`~`END` 사이만** 다시 쓰고 나머지(사람이 쓴 분석)는 그대로 둔다. 표지가 없거나 짝이 맞지 않으면 덮어쓰지 않고 실패한다.
- 입력 manifest가 하나도 없으면 생성 표 자리에 `(실행 후 기록)`을 넣는다. 수치를 추측해 채우지 않는다.
- **이 step에서 `docs/results/` 아래 날짜 파일을 만들지 마라.** 틀은 `src/kodocs/eval/templates/m2_parsing.md`에만 둔다(실행일은 사용자가 실행할 때 정해진다).
- 커밋 대상인 결과 문서에 조문·표 원문을 넣지 않는다. 대표 실패 사례는 규정 번호·조 번호·차이 유형으로 적는다.

`render_tables`가 만드는 표(각 표 위에 분모와 고정 조건을 한 줄로 적는다):
1. **측정 정보**: 생성 시각, `git rev-parse HEAD`, 파서별 `parser_version`·`settings`(`parse_runs.jsonl` 최신 기록에서), 업스테이지 응답 `model` 버전 목록, `parse_targets.json`·`infra/fontconfig/fonts.conf`·`table_golden.json` sha256.
2. **분할기 상한**: `splitter-ceiling` 행 — 구조 완전 일치율, 키 F1, 정규화 유사도. 100%가 아닌 대상 수. 분모와 함께 `excluded`(`no_law_text`) 수를 적는다.
   **제외 대상 표**: `parse_scores.json`의 `excluded_targets`(rule_id, serial, role, 사유 — 예: "법제처 본문이 이미지 한 줄"). 이 대상은 파싱·분할은 하되 조 구조·조문 텍스트 지표의 분모(전체·ok만·분할기 상한 모두)에서 뺀다고 표 위에 적는다. 표 채점은 법제처와 무관하므로 영향이 없다고 함께 적는다.
3. **조 구조**(파서별): 문서 수(ok/전체), 구조 완전 일치율, 키 정밀도·재현율, 순서 LCS, 부칙 블록 수 일치율. 전체 대상 기준과 ok만 기준을 두 표로.
4. **조문 텍스트**(파서별): 원문 비교 유사도·완전 일치율, 정규화 비교 유사도·완전 일치율(micro와 macro).
5. **편집 차이**: 파서별 `editorial_law_only`·`editorial_parsed_only`·`editorial_both` 합.
6. **표 셀 일치**(파서별): 채점된 정답 수, 셀 완전 일치율 평균, 셀 유사도 평균, 크기 일치 수. pypdf 행은 "해당 없음(표 추출 기능 없음)".
7. **처리 비용**(파서별): 총 쪽수, 총 시간, 쪽당 초, 원장 금액 합(세금 포함), 쪽당 비용. 비용은 `costs.jsonl`에서 `service`별로 합산.
8. **실패 목록**: 파서별 `failed`·`empty` 건수와 `error_type` 분포.

문서 틀(`templates/m2_parsing.md`) 목차 — 생성 표 밖의 서술은 모두 틀에 고정 문구로 쓰고, 해석이 필요한 칸은 `(실행 후 기록)`:
1. 측정 정보 (생성 표 + 사람이 채울 칸: macOS 버전, 기기·CPU/GPU, Docling 실행 장치, 측정 중 다른 부하 여부)
2. **권장 실행 순서**(아래 그대로):
   1) `make parse-pypdf` → 2) `make parse-score` → 3) `data/interim/parse_diffs/pypdf/`와 분할기 상한 결과로 분할 결과 육안 확인(분할기 규칙 문제면 여기서 고친다) →
   4) `make docling-models` → `make parse-docling` → 5) `make parse-upstage-dry`(예상 비용 확인) → 6) `make parse-upstage` →
   7) `make table-drafts` → 사람이 약 10개 검수해 `data/golden/tables/`에 확정 → `make table-golden` → `make table-score` →
   8) `make parse-score` → `make parse-report` → 9) (보조 실험) HWP 직접 입력(step 9 명령)
3. 고정 조건: 입력 PDF(M1 변환, fonts.conf 해시), 분할기·채점 코드 동일, 파서 설정(pypdf plain / Docling OCR 끔·표 구조 켬 / 업스테이지 standard·ocr auto·표 병합 끔), 대상 115개(현행 95 + 과거 20).
4. 정규화 규칙(N1~N5)과 원문 비교·정규화 비교의 차이
5. <!-- BEGIN generated --> … <!-- END generated -->
6. 대표 실패 사례 (실행 후 기록 — 규정 번호·조 번호·차이 유형으로)
7. 변환 경유 vs 직접 입력(업스테이지 보조 실험) — `(실행 후 기록)`, step 9가 표를 추가한다
8. 비용 메모: 원장 금액은 **정가 + 부가세 10% 포함**, 크레딧 미반영. 업스테이지 콘솔 사용량은 VAT 제외라 원장 금액과 다를 수 있다. 콘솔 월 한도($25)는 서비스 쪽 차단으로 원장 상한과 별개다.
9. 한계: 검색 영향(Recall@10)은 M5로 이월, 쪽수는 대체 글꼴에 좌우, 여러 쪽 표는 엄격 채점, 법제처 조문과 원본의 편집 차이.
   **법제처 본문에 조문 텍스트가 없는 버전**: `parse_scores.json`의 `law_versions_without_article_text`(수집한 전체 법제처 버전 수, 조 머리 0개인 버전 수, 버전별 rule_id·serial·현행 여부·사유)를 생성 표로 싣는다. 파싱 대상 밖 버전도 포함하며, M4 질의셋 작성 시 이 규정은 조항 단위 정답을 법제처에서 얻을 수 없다는 점을 적는다.

`Makefile`: `parse-report: uv run python -m kodocs.eval.parsing_report`, `.PHONY`에 추가. `AGENTS.md` 명령어 절: `make parse-report    # M2 파싱 결과표를 docs/results/{실행일}-m2-parsing.md에 생성·갱신 — 네트워크 없음`.

### 2. 문서 수정

**`docs/EVAL.md`** — "파싱 (실제 사규만)" 절:
- 지표 표에 행 추가: `조문 텍스트 일치 | 조 번호로 짝지은 조문의 문자 유사도·완전 일치율. 원문 비교와 정규화 비교(N1~N5)를 분리 보고`.
- `조항 구조 보존` 행 설명을 "조 수·번호·순서 일치(키 정밀도·재현율, 순서 LCS, 완전 일치율). 분할기 상한(법제처 조문을 이어 붙여 분할한 결과)을 함께 보고"로 구체화.
- `검색 영향` 행에 "M4 질의셋·M5 인덱스가 필요하므로 M5로 이월한다" 추가.
- `표 셀 일치율` 행: 정답은 HWPX 원본 `hp:tbl`에서 뽑은 파서 중립 초안을 사람이 검수해 확정, 병합 셀은 덮는 모든 칸에 복제, 표 추출 기능이 없는 파서는 "해당 없음"으로 평균에서 제외.
- 정규화 규칙 N1~N5 표와 편집 차이 집계(ADR-022) 설명을 짧은 하위 절로 추가(`text_normalize.py`의 docstring과 같은 내용).
- 표 정답 보관 규칙: `data/golden/tables/*.md`는 원문이라 gitignore, 선정 정보·파일 해시는 `data/manifest/table_golden.json`에 커밋, 해시가 바뀐 정답은 채점하지 않는다.
- "실행 조건"의 비용 문구를 `MONTHLY_BUDGET_USD` 설정값 기준으로 바꾼다.

**`docs/PRD.md`**:
- "일정과 예산"의 API 비용 문구를 정확히: `**API 비용:** 상한은 설정값(MONTHLY_BUDGET_USD). M1 실측 쪽수 기준 $45로 조정. 생성·평가·파싱 스크립트가 누적 비용을 원장(정가 + 부가세 포함)에 기록하고 상한을 넘기 전에 멈춘다.`
- M2 행 산출물: `공통 파싱 스키마, pypdf·Docling·업스테이지 어댑터, 조문 분할기(파서 무관), 조문·표 채점, 비용 원장, 파싱 결과표`.
- M2 행 사용자 작업: `업스테이지 결제 수단 확인, Docling 그룹 설치(torch 포함 대용량)·모델 다운로드(make docling-models), 파싱·채점 명령 실행(Docling 로컬 실행은 오래 걸릴 수 있음), 표 정답 약 10개 검수`.

**`docs/ARCHITECTURE.md`**:
- 디렉토리 구조: `src/kodocs/costs.py`(외부 API 비용 원장·월 상한, 모든 API 스크립트 공용) 추가. `eval/` 줄을 `src/kodocs/eval/`(평가 러너, 지표, 부트스트랩, 결과표 생성)로 옮긴다. `parsing/` 설명에 "공통 파싱 스키마, 표 셀 격자화" 추가.
- `data/` 설명: `golden/` 줄을 "질의셋·qrels·사람 채점 (커밋, 사람만 수정). **예외: `golden/tables/`(표 정답 마크다운)는 원문이라 커밋하지 않고, 선정 정보·해시를 `manifest/table_golden.json`에 커밋**"으로. `manifest/`에 비용 원장·파싱 실행 기록·채점 결과, `raw/`에 업스테이지 응답 캐시, `interim/`에 파싱 결과·diff·표 초안 추가.
- 데이터 흐름의 파서 부분을 `변환 PDF → 파서 어댑터 → 공통 파싱 결과 → 조문 분할 → 채점(법제처 조문·표 정답)`으로 구체화.

**`docs/ADR.md`** — `ADR-023: 파서 비교 조건 고정과 비용 원장` 추가:
- 결정: 입력은 M1 변환 PDF, 파서만 바꾼다. 업스테이지 `mode=standard` 고정·응답 캐시(입력 sha256)·순차 호출. 비용은 append-only 원장(정가 + 부가세 10%, 크레딧 미반영, UTC 달력 월)에 호출마다 기록하고 상한 전에 멈춘다. 표 정답은 HWPX 원본에서 파서 중립으로 만들고 원문이라 gitignore, 선정 정보만 커밋.
- 이유: auto 모드는 단가가 오를 수 있다, 재실행 재과금 방지(FP-033), 특정 파서 출력으로 정답을 만들면 그 파서에 유리, 원문 재배포 금지(ADR-002).
- 트레이드오프: 원장 금액이 콘솔 사용량(VAT 제외)과 다르다. 표 정답은 저장소만으로 재현되지 않고 해시로만 검증된다. 여러 쪽 표는 엄격 채점으로 모든 파서에 불리할 수 있다.

**`AGENTS.md`**: 기술 스택의 파싱 줄은 그대로 두고, 명령어 절에 이 phase에서 추가된 make 타깃이 모두 있는지 확인한다(앞 step들이 추가함).

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.eval.parsing_report --help
test -f src/kodocs/eval/templates/m2_parsing.md
grep -n "BEGIN generated" src/kodocs/eval/templates/m2_parsing.md
grep -n "조문 텍스트 일치" docs/EVAL.md
grep -n "M5로 이월" docs/EVAL.md
grep -n "MONTHLY_BUDGET_USD" docs/PRD.md
grep -n "table_golden.json" docs/ARCHITECTURE.md docs/EVAL.md
grep -n "ADR-023" docs/ADR.md
test -z "$(ls docs/results/*m2-parsing* 2>/dev/null)"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 결과 문서 틀에 지어낸 수치가 없는가? 수치 칸이 모두 `(실행 후 기록)` 또는 생성 표인가?
   - 생성기가 사람이 쓴 부분(표지 밖)을 덮어쓰지 않는가(테스트)?
   - 측정 정보에 날짜·커밋·도구·모델 버전·고정 조건·측정 환경이 있는가(FP-044)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 결과 수치를 지어내거나 예시 수치를 채우지 마라. 이유: EVAL.md 결과 보고 규칙, 실행 후 사용자가 생성한다.
- `docs/results/` 아래에 날짜 파일을 만들지 마라. 이유: 실행일이 아직 없고, 날짜가 틀린 결과 문서가 남는다.
- `make parse-report`를 실행하지 마라(`--help` 제외). 이유: FP-042.
- 결과 문서·생성 표에 조문·표 원문을 넣지 마라. 이유: ADR-002.
- 측정 환경(기기·도구 버전·설정 해시) 칸을 빼지 마라. 이유: FP-044.
- 기존 ADR의 결정 문구를 이 step에서 바꾸지 마라(ADR-008 갱신은 step 9). 새 ADR-023만 추가한다.
- `data/golden/`을 만들거나 수정하지 마라.
- 기존 테스트를 깨뜨리지 마라.
