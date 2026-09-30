# Step 2: current-first

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/retrieval/search.py`(`ChunkHit`·`DocHit`·`SearchResult`·`_SOURCE_FIELDS`·`authorized_search`), `src/kodocs/retrieval/lookup.py`(`authorized_lookup_id`, `_group_documents`), `src/kodocs/retrieval/index_schema.py`(매핑: `rule_id`·`is_current`·`title`·`text_nori`·`date`)
- `src/kodocs/retrieval/index_build.py` 600~630행 부근(색인 문서 필드) — `is_current`·`rule_id`가 어떻게 채워지는지 확인
- `src/kodocs/eval/m5_runner.py`, `src/kodocs/eval/m5_analysis.py`(temporal 개정 전/현행 순위 계산), `src/kodocs/eval/m5_inputs.py`, `data/manifest/m5_eval_inputs.json`, `data/manifest/m5_prior_versions.json`
- `docs/results/2026-09-30-m5-retrieval.md`의 'temporal 개정 전/현행 순위' 표와 'M6로 넘기는 결정 (b)'
- `tests/retrieval/`의 기존 search·lookup 테스트(fake client 사용 방식)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

검색 계층에 **`is_current` 우선 처리 옵션**과 도구 계층이 쓸 **반환 필드 확장**을 넣는다. 랭킹·점수·후보 집합은 M5와 같아야 하고, 옵션이 꺼져 있으면 M5와 완전히 같은 동작이다.

### 1. 반환 필드 확장 (랭킹 불변)
- `_SOURCE_FIELDS`에 `text_nori`(청크 본문), `title`, `is_current`, `rule_id`, `date`를 추가하고 `ChunkHit`에 `text: str | None = None`, `title: str | None = None`, `is_current: bool | None = None`, `rule_id: str | None = None`를 **기본값 있는 필드로** 추가한다(기존 M5 코드·테스트가 위치 인자로 만들어도 깨지지 않게). `DocHit`(`lookup.py`가 만드는 문서 묶음 포함)에도 `title`·`is_current`·`rule_id`를 같은 방식으로 추가한다. 응답에 필드가 없으면 `None`이다(필수로 만들지 마라).
- 검색 쿼리 본문의 `query`·`knn`·pipeline·`access_filter`는 바꾸지 않는다. `_source` 목록만 넓힌다.

### 2. `prefer_current` 옵션
- `authorized_search(..., prefer_current: bool = False)`와 `authorized_lookup_id(..., prefer_current: bool = False)`. 공통 순수 함수 `prefer_current_order(items, *, key_rule, key_is_current)`를 `retrieval/ordering.py`에 둔다.
- 규칙(ADR-029 7번): `rule_id`가 같은 항목 그룹 중 **현행(`is_current=True`)과 개정 전(`is_current=False`)이 함께 있는 그룹**에서만 동작한다. 그 그룹의 항목들이 차지한 **위치 집합은 그대로 두고**, 그 위치들에 현행 항목(원래 상대 순서 유지)을 먼저, 개정 전 항목(원래 상대 순서 유지)을 나중에 채운다. 그룹 밖 항목의 위치·점수는 바뀌지 않는다. `rule_id`나 `is_current`가 `None`인 항목은 건드리지 않는다.
- 청크 목록과 문서 목록에 각각 같은 규칙을 적용한다. 반환 청크 수·문서 수는 그대로다.
- 테스트(fake client): (a) 옵션 꺼짐이면 결과가 이전과 동일, (b) 개정 전이 위인 그룹에서 현행이 앞으로 오고 다른 문서 위치는 불변, (c) 현행만 또는 개정 전만 있는 그룹은 불변, (d) 서로 다른 `rule_id`끼리는 교환하지 않음, (e) `rule_id=None` 합성 문서는 불변, (f) 모든 검색 요청 본문에 `access_filter`가 그대로 들어 있음.

### 3. `make m6-temporal` (사용자 실행, LLM 없음)
- `src/kodocs/eval/m6_temporal.py`(파일 이름 `m6_` 접두). M5 최종 구성(`m5_config.json`)으로 골드 temporal 5문항(dev 2 + test 3)을 `prefer_current` 끔/켬 두 번 검색해 문항별 현행 문서 순위·개정 전 문서 순위·개정 전 우선 여부를 기록한다(순위·문서 계산은 `m5_analysis`/`m5_runner`의 기존 함수를 재사용한다. 새로 구현하지 마라).
- 또한 골드 test·dev와 실버 test·dev의 **검색 지표 대상 문항 전부**에 대해 끔/켬의 상위 10 문서 목록이 바뀐 문항 ID를 센다. 바뀐 문항은 temporal 5문항 안에만 있어야 하며, 밖에서 바뀐 문항은 `unexpected_changes`로 기록한다.
- 결과는 `data/manifest/m6_temporal.json`(문항 ID·순위·개수·M5 config_hash만, 조문·본문 텍스트 없음)에 쓴다. 진행은 `flush=True`로 출력한다(FP-034).
- 이 명령은 실행 중인 OpenSearch와 로컬 BGE-M3가 필요하다. 이 step에서는 fake client·fake embedder로만 테스트하고 실제 실행하지 않는다. M5 `m5_test` 결과·잠금·`m5_test_runs.jsonl`을 읽기만 하고 절대 쓰지 않는다.
- `Makefile`에 `m6-temporal` 타깃(`uv run python -m kodocs.eval.m6_temporal`)을 추가하되 `verify` 타깃에는 넣지 않는다. `AGENTS.md` 명령어 목록에 `make m6-temporal  # M6 is_current 우선 처리 전후 순위·비 temporal 불변 확인 → data/manifest/m6_temporal.json — OpenSearch 필요(make up), 사용자 실행 (LLM 없음)`를 추가한다. 타깃 선언 테스트(`tests/test_m5_make_targets.py` 스타일)를 `tests/test_m6_make_targets.py`로 만든다.

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
uv run pytest -q tests/retrieval -k "current or ordering"
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - `prefer_current` 기본값이 False라서 M5 실행 경로의 결과가 그대로인가(기존 M5 테스트 전부 통과)?
   - `data/manifest/m5_config.json`과 `m5_test_*` 파일이 수정되지 않았는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `SearchParams`에 `prefer_current` 필드를 추가하거나 M5 설정 해시 계산에 영향을 주지 마라. 이유: 검색 구성 해시(455f063e…)가 바뀌면 M5 결과와 잠금이 무효가 된다. 옵션은 함수 인자로만 둔다.
- 점수 가중(현행 부스트)으로 구현하지 마라. 이유: 규칙 15는 같은 rule_id의 순서 교환만 허용하며 후보 집합·점수·다른 문서 순위를 바꾸면 M5와 비교 불가가 된다.
- `make m5-test`를 실행하거나 M5 결과를 재계산해 덮어쓰지 마라. 이유: 규칙 16은 M5 판정을 소급 변경하지 않는다.
- 통합 테스트가 실측 인덱스(`kodocs-m5-*`)를 만들거나 지우게 하지 마라. 이유: M5에서 통합 테스트가 실측 인덱스를 삭제해 재색인해야 했다(FP-046). 새 통합 테스트는 읽기 전용이거나 uuid 임시 이름만 쓴다.
- temporal 문항의 정답이 옛 판일 때를 억지로 처리하는 코드를 넣지 마라. 이유: 골드 temporal 5문항의 정답은 전부 현행이며, 옛 판을 묻는 질문은 한계로 기록하기로 했다(규칙 16).
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
