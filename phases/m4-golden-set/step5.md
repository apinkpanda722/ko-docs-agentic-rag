# Step 5: real-items

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 원문(`data/raw/`)·정제 산출물(`data/interim/`) 비커밋, 법제처 담당자 정보·인증키를 남기지 않음
- `docs/ADR.md` — **ADR-002**(커밋: 규정명·조 번호·조 제목·별표 제목 등 80자 이하 구조 메타데이터, 비커밋: 조문·별표 본문), ADR-016, ADR-022, **ADR-025**
- `docs/EVAL.md` — 최신성 문항(개정 이력, 버전 매칭 정확·근사만)
- step 1: `src/kodocs/eval/golden.py`(`Question`, `Qrel.article_key`·`annex_id`, `validate_question`의 `unknown_article`·`unknown_annex`·`real_text_fields`)
- step 2: `src/kodocs/eval/golden_specs.py`(unanswerable 명세의 `no_answer_checks`)
- `src/kodocs/parsing/articles.py` — `law_to_lines`, `split_text`(법제처 조문 → 조 키 분할)
- `data/manifest/regulation_refs.json` — 규정명 → `{articles: {조 키: 조 제목}, rule_id, serial}`
- `data/manifest/version_matches.jsonl` — `confidence`(exact/approximate/failed), `law_serial`, `law_issued_date`, `law_rule_id`
- `data/manifest/table_golden.json` — 확정 표 10개(`id`, `rule_no`, `annex_label`)
- `data/manifest/law_api.jsonl` — 법제처 버전 목록(현행·연혁)
- 로컬 전용: `data/interim/law/{rule_id}/{serial}.json`(필드 `name`, `serial`, `rule_id`, `issued_date`, `is_current`, `articles`(문자열 목록), `annexes`). **없을 수 있다.** 테스트는 이 경로에 의존하지 마라.

## 배경

실제 규정 골드 20문항(identifier 2·semantic 4·table 6·multi_doc 2·temporal:revision 5·unanswerable 1)은 Claude가 세션에서 로컬 조문을 읽어 초안을 쓰고 사람이 확정한다(ADR-025). 이 step은 그 작업을 돕고 검증하는 도구만 만든다: 후보 목록, 로컬 작성 양식, 초안 검증과 커밋 가능한 투영(projection), 답 없음 ③(실제 규정 본문 검색). 조문 본문은 절대 커밋 경로로 나가면 안 된다.

## 작업

TDD: `tests/eval/test_golden_real.py`를 먼저 작성한다. 법제처 정제 파일 fixture는 `tmp_path`에 **짧은 가짜 규정**(가짜 규정명·가짜 조문)으로 만든다.

### `src/kodocs/eval/golden_real.py`

```python
INTERIM_DIR = Path("data/interim/golden")                 # 비커밋
CANDIDATES_PATH = INTERIM_DIR / "real_candidates.md"      # 비커밋(조 제목·표 라벨·개정 쌍, 본문 발췌 가능)
DRAFTS_PATH = INTERIM_DIR / "real_drafts.jsonl"           # 비커밋: Claude 초안(참조 답변 포함)
PROJECTION_PATH = Path("data/synthetic/queries/real_projection.jsonl")   # 커밋: 참조 답변 제거, reference_sha256만
SUMMARY_PATH = Path("data/manifest/golden_real.json")     # 커밋, 결정론
REAL_QUOTA = {"identifier": 2, "semantic": 4, "table": 6, "multi_doc": 2, "temporal": 5, "unanswerable": 1}
def law_versions(root: Path) -> list[dict]: ...           # interim 파일 메타데이터(본문 제외)
def article_texts(law: Mapping) -> dict[str, str]: ...    # law_to_lines + split_text로 조 키 → 조문(메모리에서만)
def revision_pairs(root: Path) -> list[dict]: ...         # 같은 rule_id의 연속 버전 중 양쪽 모두 version_matches confidence ∈ {exact, approximate}이고 같은 조 키의 조문이 다른 쌍
def search_law_texts(root: Path, terms: Sequence[str]) -> list[tuple[str, str]]: ...   # (doc_id "law-{serial}", 조 키) — 모든 term이 같은 조에 함께 나오는 곳
def write_candidates(root: Path) -> int: ...              # CANDIDATES_PATH 작성
def check_drafts(root: Path) -> int: ...                  # DRAFTS_PATH 검증 → PROJECTION_PATH·SUMMARY_PATH
def main(argv=None) -> int: ...   # python -m kodocs.eval.golden_real {candidates|check}
```

**후보 목록(`candidates`)**
- 현행 규정 조 키·조 제목 목록(색인 기반), 확정 표 10개(`table_golden.json`), 최신성 후보 개정 쌍(`revision_pairs`: rule_id, 이전·이후 serial·발령일자, 달라진 조 키). 로컬에만 쓰므로 조문 발췌를 넣어도 된다.
- `data/interim/law/`가 없으면 "법제처 정제 파일 없음, `make collect-law` 먼저(사용자 실행)" 안내 후 종료 코드 1.

**초안 형식(`real_drafts.jsonl`, 한 줄 = `golden.Question` dict + `reference_answer`)**: id `g-real-NNN`, `set="gold"`, `source="real"`, qrels는 `law-{serial}` + `article_key`(표는 `annex_id` = `table_golden` id, 필요 시 조 키 병기), `summary80`(80자 이하 요지), temporal은 태그 `temporal:revision` + `as_of`(기준 버전의 발령일자), unanswerable은 qrels 없음.

**검증(`check`)** — 각 규칙 fixture 테스트 필수:
- 모든 문항이 `golden.validate_question`을 통과(색인에 조 키가 있는지, 요지 80자 이하 등).
- `REAL_QUOTA`를 정확히 채우는지 유형별로 보고. 부족·초과면 종료 코드 1(FP-043).
- temporal 문항의 qrels serial과 `as_of`가 `revision_pairs` 중 하나와 맞고, 그 쌍의 두 버전 모두 confidence exact/approximate인지(EVAL).
- unanswerable 문항: 질문의 핵심어(초안의 `no_answer_terms` 필드)로 `search_law_texts` 결과가 0건이어야 한다(답 없음 ③). 결과를 `no_answer_checks.real = true/false`로 기록.
- 합성 unanswerable 명세(`data/synthetic/queries/specs.jsonl`)에도 같은 ③ 검사를 돌려 결과를 `SUMMARY_PATH`의 `synthetic_no_answer_real_check: {id: bool}`에 기록한다(specs 파일 자체는 수정하지 않는다). `no_answer_terms`는 명세의 주어·속성 핵심어를 쓴다.
- **투영**: `reference_answer`를 빼고 `reference_sha256`(참조 답변 UTF-8의 SHA-256)을 채워 `PROJECTION_PATH`에 쓴다. 투영의 어떤 문자열 필드에도 조문 본문 조각이 들어가지 않았는지 검사한다: 각 문항의 질문·요지와 해당 조문 사이 최장 공통 부분 문자열이 30자를 넘으면 실패(`real_text_leak`). 이유: ADR-002(조문 본문 비커밋).
- `SUMMARY_PATH`: `{counts, quota_diff, no_answer_checks, revision_pairs_available, inputs: {regulation_refs_sha256, version_matches_sha256, table_golden_sha256, law_files_sha256(파일 해시 목록의 해시), drafts_sha256}}`(시각·본문 없음).

### 원문 비커밋 가드

- `.gitignore`에 `data/interim/`이 이미 있는지 확인하고, 없으면 추가하지 말고 blocked로 보고하라(비커밋 전제가 깨진 상태다).
- 테스트: `PROJECTION_PATH`가 있으면 모든 행에 `reference_answer` 키가 없고 `reference_sha256`이 64자 16진수임을 검사한다.

### Makefile·AGENTS.md

- `golden-real-candidates: uv run python -m kodocs.eval.golden_real candidates`
- `golden-real-check: uv run python -m kodocs.eval.golden_real check`
- AGENTS.md:
  - `make golden-real-candidates  # 실제 규정 문항 후보(조 제목·확정 표·개정 쌍) → data/interim/golden/real_candidates.md (로컬 전용, 네트워크 없음)`
  - `make golden-real-check       # data/interim/golden/real_drafts.jsonl 검증(할당·조 키·개정 쌍·답 없음 ③·본문 누출) → 커밋용 투영 data/synthetic/queries/real_projection.jsonl (네트워크 없음)`

## Acceptance Criteria

```bash
make verify
grep -n "golden-real-candidates\|golden-real-check" Makefile AGENTS.md
git check-ignore -q data/interim/golden/real_drafts.jsonl && echo "interim ignored"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `data/interim/law/`가 로컬에 있어도 `real_drafts.jsonl`을 만들거나 초안을 쓰지 마라(초안은 사용자 지시로 Claude가 세션에서 쓴다).
2. 아키텍처 체크리스트를 확인한다:
   - 조문 본문이 커밋 경로(`data/synthetic/`, `data/manifest/`, `data/golden/`)로 나가는 경로가 없는가(ADR-002)? 누출 검사가 테스트로 강제되는가?
   - 최신성 후보가 버전 매칭 exact/approximate만 쓰는가(EVAL)?
   - 법제처 파일이 없을 때 조용히 0건 성공으로 끝나지 않는가(FP-031)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 실제 규정 문항 초안(`real_drafts.jsonl`)을 만들지 마라. 이유: 초안은 사용자 지시로 Claude가 세션에서 쓰고 사람이 확정한다(ADR-025).
- 조문·별표 본문을 커밋 경로의 파일이나 테스트 fixture에 넣지 마라. fixture는 가짜 규정으로 만든다. 이유: ADR-002.
- 실제 규정 텍스트를 외부 API로 보내지 마라. 이유: ADR-025(실제 문항은 API로 보내지 않음).
- 법제처 수집(`make collect-law`)이나 네트워크 요청을 하지 마라. 이유: FP-040, 인증키 필요.
- `data/interim/law/`의 파일을 수정하지 마라. 이유: 정답 텍스트다(FP-020).
- 기존 테스트를 깨뜨리지 마라.
