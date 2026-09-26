# Step 6: parse-targets

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 명령어 절
- `docs/ADR.md` — ADR-008, ADR-022(파싱 대상 제한: 업스테이지 페이지당 과금)
- `docs/EVAL.md` — 파싱 지표, 결과 보고 규칙
- `docs/PRD.md` — 마일스톤 M1·M2 행
- step 2 산출물: `src/kodocs/ingest/law_api.py` — `data/manifest/law_api.jsonl`(`rule_id`, `serial`, `issued_date`, `is_current`, `annex_count`, `annex_titles`)
- step 3 산출물: `src/kodocs/ingest/alio.py` — `data/manifest/alio.jsonl`
- step 4 산출물: `src/kodocs/ingest/matching.py` — `data/manifest/version_matches.jsonl`(`law_serial`, `confidence`, `parse_eval_eligible`)
- step 5 산출물: `src/kodocs/ingest/convert.py` — `data/manifest/conversions.jsonl`(`source_sha256`, `output_path`, `page_count`, `status`, `embedded_fonts`, `hangul_distinct`), `infra/fontconfig/fonts.conf`(HWP 지정 글꼴 → 함초롬 대체 규칙)
- `tests/ingest/`, `Makefile`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

업스테이지 Document Parse는 페이지당 과금이라 전 버전을 파싱하지 않는다(ADR-022). 이 step은 **네트워크 없이** manifest만 읽어
M2 파싱 대상 목록을 예상 페이지 수와 함께 만든다. 또 M1 결과 리포트를 쓴다. 리포트에는 수집·매칭·변환 절차와,
**ALIO HWP 안에 별표가 포함돼 있는지 확인하는 방법**을 남긴다.

## 작업

TDD: `tests/ingest/test_parse_targets.py`를 먼저 작성한다. 입력은 테스트 안에서 만든 가짜 manifest다.

### 1. `src/kodocs/ingest/parse_targets.py`

```python
@dataclass(frozen=True)
class ParseTarget:
    rule_no: str
    law_rule_id: str
    law_serial: str
    issued_date: str
    role: str                  # "current" | "history"
    source_path: str           # ALIO 원본 (저장소 상대)
    source_sha256: str
    source_kind: str           # hwp5 | hwpx
    pdf_path: str | None       # 변환 성공 시
    page_count: int | None     # 변환 전이면 None
    match_confidence: str      # exact | approximate
    law_annex_count: int       # 법제처 기준 별표 수 (별표 포함 여부 확인용)

def select_targets(law, alio, matches, conversions, *, history_rules: int = 10, versions_per_rule: int = 2) -> TargetSelection: ...
def run(root: Path, *, history_rules: int = 10, versions_per_rule: int = 2) -> TargetSelection: ...
def main(argv: list[str] | None = None) -> int: ...   # python -m kodocs.ingest.parse_targets
```

선정 규칙(결정론, 테스트로 고정):
- **현행본 전부**: 법제처 현행 버전(`is_current=true`)마다, 그 `serial`에 매칭된(`parse_eval_eligible=true`) ALIO 파일 하나.
  같은 버전에 파일이 여럿이면 HWPX > HWP5, 그다음 `file_no`가 큰 것을 고른다. 매칭된 파일이 없는 현행 규정은
  `missing_current` 목록에 규정명과 이유로 남긴다(조용히 빼지 않는다).
- **과거 버전**: 법제처 연혁 기준 버전 수가 **3개 이상**인 규정 중, 과거 버전에 매칭된 eligible 파일이 있는 규정을
  (버전 수 내림차순, `rule_id` 오름차순)으로 정렬해 앞에서 `history_rules`개를 고른다. 규정마다 과거 버전 중
  **가장 오래된 것과 가장 최근 과거 버전**부터 `versions_per_rule`개를 고른다(1개면 가장 최근 과거 버전).
- `confidence=failed`는 절대 포함하지 않는다.
- 페이지 수는 `conversions.jsonl`의 `status=ok` 레코드를 `source_sha256`으로 찾는다. 없으면 `page_count=None`, 요약에 "변환 전" 수로 센다.
- **manifest는 `kodocs.ingest.manifest.latest_records`로 읽는다**(키마다 마지막 줄). 키: 법제처 `serial`, ALIO `(source, file_no, zip_member_path)`, 변환 기록 `source_sha256`. `version_matches.jsonl`은 매번 새로 쓰므로 그대로 읽는다.

### 2. 출력

- `data/manifest/parse_targets.json` (커밋 대상, 실행마다 새로 씀):
  ```json
  {"generated_at": "...", "params": {"history_rules": 10, "versions_per_rule": 2},
   "totals": {"targets": 0, "current": 0, "history": 0, "pages_known": 0, "targets_without_page_count": 0},
   "missing_current": [{"rule_id": "...", "name": "...", "reason": "..."}],
   "targets": [ ...ParseTarget dict... ]}
  ```
  `targets`는 (`role`, `rule_no`, `issued_date`)로 정렬한다. 원문 텍스트·담당자 정보는 넣지 않는다(메타데이터만).
- 표준 출력 요약: 대상 수(현행/과거), 알려진 총 페이지 수, 페이지 수 미상 대상 수, 현행 누락 규정 수.
- `--history-rules`, `--versions-per-rule` 옵션으로 사용자가 예산에 맞춰 조정한다.
- 입력 manifest가 없으면 먼저 실행할 명령(`make collect-law`, `make collect-alio`, `make match-versions`, `make convert-hwp`)을 안내하고 0이 아닌 코드로 끝난다.

### 3. Make 타깃

- `Makefile`에 `parse-targets` 타깃을 추가하고 `.PHONY`에 넣는다. `verify`는 건드리지 않는다.
- `AGENTS.md` 명령어 절에 `make parse-targets  # M2 파싱 대상 목록(현행 전부 + 과거 버전 일부, 예상 페이지 수) (네트워크 없음)`을 추가한다.

### 4. M1 결과 리포트: `docs/results/m1-data-collection.md`

수치는 사용자가 수집을 실행한 뒤 채우므로, **절차와 방법을 쓰고 수치 칸은 비워 둔다**(`(실행 후 기록)`). 수치를 지어내지 마라. 목차:

1. **목적과 범위** — NIA 내부규정 95개, 두 경로 수집(ADR-022), 원문 무가공·로컬 전용(ADR-002).
2. **재현 절차** — 순서: `.env`에 `LAW_OPEN_API_KEY` 입력 → `make collect-law` → `make collect-alio` → `make match-versions`
   → `make convert-hwp`(도구 점검 포함) → `make parse-targets`. 각 명령이 만드는 파일(`data/raw/…`, `data/interim/…`, `data/manifest/…`).
3. **manifest 설명** — 파일별 한 줄 설명과 핵심 필드. 커밋되는 것(manifest)과 안 되는 것(raw, interim).
4. **버전 매칭 규칙** — step 4 규칙 표(정확/근사/실패)와, 실패 파일은 파싱 평가에서 제외된다는 점.
5. **파싱 대상 선정 규칙** — 위 선정 규칙과 조정 옵션.
6. **ALIO HWP 안의 별표 포함 여부 확인 방법** — 아래 내용을 절차로 쓴다:
   - 기준 목록: 법제처 정제 JSON(`data/interim/law/{rule_id}/{serial}.json`)의 `annexes`(별표번호·가지번호·구분(별표/별지)·제목)가 그 버전에 있어야 할 별표다.
     `parse_targets.json`의 `law_annex_count`가 0이면 확인할 필요가 없다.
   - 별도 파일 여부: 같은 규정의 ALIO `bFiles` 파일명(manifest `file_name`)에 `별표`·`별지`·`서식`이 있으면 별표가 본문과 따로 첨부된 경우다.
   - HWPX: zip 안 `Contents/section*.xml`의 텍스트(`hp:t` 요소)에서 `[별표 N]`, `별표 N`, `[별지 제N호`, 그리고 법제처 별표 제목 문자열을 찾는다.
     표 존재는 `hp:tbl` 요소 수로 본다.
   - HWP 5.x: `olefile`(M1에서 형식 판정에 이미 쓰는 의존성)로 OLE 스트림 `BodyText/Section{N}`을 읽는다(`FileHeader` 속성의 압축 비트가 켜져 있으면 raw deflate, `zlib.decompress(data, -15)`).
     레코드 헤더(4바이트: 태그 ID 하위 10비트, 크기 상위 12비트, 크기 0xFFF면 다음 4바이트가 크기)를 따라가며
     `HWPTAG_PARA_TEXT`(태그 67)의 UTF-16LE 텍스트에서 위 패턴을 찾는다. 표는 `HWPTAG_TABLE`(태그 77)로 센다. (`PrvText` 스트림은 앞부분 미리보기만 있어 쓰지 않는다.)
     구현 부담이 크면 변환 PDF의 텍스트(pypdf)에서 같은 패턴을 찾는 방법으로 대신하고, 변환 손실 가능성을 함께 적는다.
   - 판정: 법제처 별표 항목마다 `본문 포함` / `별도 파일` / `찾지 못함`으로 분류해 M2 표 정답 후보(PRD: 별표 표 약 10개)를 고른다.
   - 이 확인은 M2에서 파서 비교 전에 수행한다. M1은 방법만 남긴다.
7. **결과 수치** — 표 틀만: 법제처 현행·연혁 버전 수, ALIO 규정·파일 수(형식별: hwp5·hwpx·hwp3·ole_other·zip·pdf, zip 멤버), 다운로드 실패 수, 매칭 신뢰도별 파일 수,
   변환 성공·실패 수, **HCR 외 글꼴이 섞인 파일 수**(`conversions.jsonl` 최신 `ok` 레코드 중 `embedded_fonts`에 `HCR`로 시작하지 않는 글꼴이 하나라도 있는 파일, 섞인 글꼴 이름별 파일 수 포함), `hangul_distinct`가 0인 파일 수, 파싱 대상 수와 예상 페이지 수. 값은 `(실행 후 기록)`.
   `parse_targets.json`의 각 대상에도 `embedded_fonts`와 `non_hcr_fonts`(HCR 외 글꼴 목록)를 넣어 M2에서 거를 수 있게 한다.
8. **한계** — 원본이 지정한 글꼴(휴먼명조·HY견고딕 등 상용/Windows 글꼴)은 `infra/fontconfig/fonts.conf` 규칙에 따라 함초롬(HCR Batang/HCR Dotum)으로 대체된다. 함초롬을 설치하고 대체 규칙을 적용했을 때 샘플 2건의 쪽수가 GUI 변환과 같았다. 규칙에 없는 글꼴을 쓰는 파일은 `embedded_fonts`에 HCR 외 글꼴이 섞여 드러난다. 폐지 규정과 이름이 같은 현행 규정은 규정 매칭이 "모호함"으로 실패할 수 있다(현행 우선 규칙은 넣지 않았고, 실측한 "규정 매칭 실패" 목록을 원인별로 본 뒤 판단한다). HWP 3.x·구형 OLE(.doc 등) 파일은 H2Orestart 미지원·비대상이라 변환·파싱에서 빠진다(형식별 수로 보고), 파일명 날짜의 불규칙성, 매칭 실패 파일 제외에 따른 과거 버전 표본 감소, 법제처 조문과 ALIO 원본의 편집 차이 가능성.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.ingest.parse_targets --help
test -f docs/results/m1-data-collection.md
grep -n "별표" docs/results/m1-data-collection.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 네트워크 요청이 없는가? `failed` 매칭이 대상에 들어갈 수 없는가(테스트)?
   - `parse_targets.json`에 원문 텍스트·담당자 정보가 없는가?
   - 리포트에 지어낸 수치가 없는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 수집·매칭·변환 명령을 실제로 실행하지 마라. 이유: 입력 데이터가 없고, 실제 호출은 사용자가 실행한다(ADR-014).
- 리포트에 수치를 지어내지 마라. 이유: 결과 보고 규칙(EVAL), 실행 후 사용자가 기록한다.
- 별표 확인 도구(HWP 레코드 파서 등)를 이 step에서 구현하지 마라. 이유: 사용자는 **방법**을 리포트에 남기라고 했고, 구현은 M2 범위다.
- 업스테이지·기타 파서를 호출하거나 비용을 계산하는 코드를 만들지 마라. 이유: 가격은 M2에서 확인한다. 이 step은 페이지 수까지다.
- `data/golden/`을 만들거나 수정하지 마라.
- 기존 테스트를 깨뜨리지 마라.
