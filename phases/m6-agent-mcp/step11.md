# Step 11: m6-report

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `src/kodocs/eval/m5_report.py`, `src/kodocs/eval/templates/m5_retrieval.md`, `docs/results/2026-09-30-m5-retrieval.md` — 결과 문서 생성 영역(`<!-- END generated -->`)과 보존 영역 패턴
- `src/kodocs/eval/m6_*.py`(step 2·10 산출물), `data/manifest/m6_eval_inputs.json`·`m6_temporal.json`·`m6_dev.json`·`m6_config.json`·`m6_test_results/`·`m6_test_runs.jsonl`·`m6_dry_runs.jsonl` (아직 없을 수 있음 — 없으면 해당 절에 '미실행'으로 표시)
- `docs/results/hypotheses.md` — M6 판정 규칙, `docs/EVAL.md` 결과 보고 절, `Makefile`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

M6 결과 문서 생성기를 만든다(`make m6-report`, 네트워크 없음). 결과 파일명은 **`docs/results/2026-10-01-m6-agent-mcp.md`로 고정**(실행 날짜와 무관, M5와 같은 방식)이다. M5 `m5_report`의 패턴(생성 영역과 보존 영역 분리, 갱신 시 보존 영역 유지)을 따른다.

### 1. `src/kodocs/eval/m6_report.py` + `src/kodocs/eval/templates/m6_agent.md`
템플릿의 **보존 영역**(생성기가 덮어쓰지 않음):
- 목적과 범위, 고정 조건(M5 최종 구성·config_hash `455f063e…`, ADR-029 숫자, 답변 모델 후보·설정, 가드레일), 측정 환경 기록 칸(기기·OpenSearch 힙·임베딩 CPU/GPU·동시성·날짜·커밋 해시·도구 버전, FP-044),
- **사용자 실행 절차**: (1) `make up`, M5 인덱스 4개 확인 (2) `make m6-inputs` (3) `make m6-tokens` (4) `make m6-temporal` (5) `make test-integration` (6) `make m6-dry` → 예상 비용 확인 (7) `LIMIT=5 make m6-dev`(선택, 파일럿) → `make m6-dev` (8) `make m6-freeze` (9) `make m6-test` → `make m6-report` (재실행은 `RERUN_REASON="사유" make m6-test`) (10) **OpenAI 콘솔 Usage를 실행 전후로 기록해 원장과 대조**(FP-037) (11) **결과 산출물·문서를 커밋하기 전에 `make verify`가 통과하는지 확인**(M5 결과 PR에서 순서가 바뀐 적 있음),
- **사전 등록 요약**: 규칙 1~23 요약과 hypotheses.md 링크. H5는 이 phase에서 판정하지 않고 M7(Gemini 채점) 몫임을 명시.
- **한계**(반드시 포함): 시스템·문항당 1회 실행이라 비결정성이 결과에 섞임(temperature 미지원 모델, effort low), 가드레일·`billing_uncertain` 실패를 실패로 세는 규칙이 호출이 많은 Agentic에 불리할 수 있음, 골드 temporal 5문항의 정답은 모두 현행이라 `prefer_current`가 **날짜를 명시해 옛 판을 묻는 질문**을 처리하지 못함, dev 비교의 정답 값 포함률은 값이 있는 문항(56)만 재며 실제 문항은 3개뿐이라 출처별 결론은 참고용, dev 조정·비교에 쓴 문항과 test는 분리되어 있으나 표본이 작아 CI가 넓음, MCP 인증은 데모용 정적 토큰(OAuth 2.1 아님), OpenAI 원장은 콘솔 청구와 첫 실행 뒤 대조 전까지 미검증, 지연은 동시성 4 실행이라 P95 판정에 쓰지 않음.

**생성 영역**(데이터 파일에서 재계산, 파일이 없으면 '미실행'):
- 입력 요약(`m6_eval_inputs.json`): dev 75·H5 풀 13·정답 값 분모 56(출처별)·게이트 8·라우터 유형 표(문항 8개 이상 유형과 미만 유형).
- `is_current` 효과(`m6_temporal.json`): temporal 5문항 전후 순위 표(옵션 끔/켬), 개정 전 우선 문항 수(M5는 2/5), 비 temporal 문항 불변 확인·`unexpected_changes`. **사후 기술 통계이며 M5 판정을 바꾸지 않는다**고 명시.
- dev 모델 비교(`m6_dev.json`): 모델×시스템 정답 값 포함률(분모 56, 출처별), 게이트 (a)(b) 결과, unanswerable 거절률, 비용·지연 P50·동시성, 짝 비교 차이·95% CI, 선택 결과와 사유.
- 고정 설정(`m6_config.json`): 모델·해시·`config_hash`·고정 시각.
- test 실행(`m6_test_results/`·`m6_test_runs.jsonl`): 첫 실행·재실행 표(시도·시각·상태·사유·이전/이번 결과 해시), 시스템별 결정론 지표(정답 값 포함률[값 있는 문항 한정], 인용 정밀도, 거절률), 비용·단계 수·지연 P50·가드레일·`billing_uncertain` 수. H5 풀 13문항은 '채점 대기(M7)'로 표시하고 **정답성 수치를 만들지 않는다**.
- 예상 비용 대 실제 비용(`m6_dry_runs.jsonl`·원장).
- MCP 계약: 도구별 버전과 스냅샷 해시(`tests/contract/snapshots/index.json`), 통합 테스트 결과는 절차 안내만(자동 수집하지 않음).

### 2. Makefile·AGENTS.md
`Makefile`에 `m6-report`(`uv run python -m kodocs.eval.m6_report`)를 추가하고(`verify`에는 넣지 않음) `AGENTS.md`에 `make m6-report    # M6 결과 문서 docs/results/2026-10-01-m6-agent-mcp.md(고정) 갱신 — 네트워크 없음`을 추가한다. `tests/test_m6_make_targets.py`에 선언 테스트를 더한다.

### 3. 테스트
- 입력 파일이 하나도 없는 상태에서도 문서가 생성되고 각 절이 '미실행'으로 표시됨. 일부 파일만 있어도 그 절만 채워짐.
- 보존 영역 편집이 재생성 후에도 유지되고 생성 영역만 바뀜(2회 실행 결과가 동일한 멱등성).
- 문서에 `answer_values`·질문 원문·답변 텍스트·실제 규정 조문·토큰·키가 들어가지 않음. H5 절에 정답성 수치가 없고 '채점 대기(M7)'만 있음.
- 결과 문서에 날짜(파일명)·커밋 해시·모델 ID·설정 해시·측정 환경 칸이 있음(FP-044).
- 이 step에서 실제 `docs/results/2026-10-01-m6-agent-mcp.md`를 커밋 대상으로 생성해도 된다(입력이 없으면 전부 '미실행'인 골격). 단 M5 결과 문서는 수정하지 않는다.

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
uv run pytest -q tests/eval -k m6_report
make m6-report
git diff --stat -- docs/results/2026-09-30-m5-retrieval.md   # 출력이 비어 있어야 한다(M5 결과 문서 불변)
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - H5 절이 '채점 대기(M7)'만 표시하고 정답성 수치를 만들지 않는가?
   - 결과 문서 생성이 멱등이고 보존 영역이 유지되는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- Gemini 채점·정답성 계산·H5 판정 로직을 만들지 마라. 이유: 이 phase는 판정 입력만 만들고 H5는 M7에서 채점 후 판정한다(규칙 3).
- `docs/results/2026-09-30-m5-retrieval.md`를 수정하지 마라. 이유: M5 결과는 소급 변경하지 않는다(규칙 16).
- 결과 문서·manifest에 답변 텍스트·질문 원문·조문 원문·평문 토큰을 넣지 마라. 이유: 원문은 로컬 전용이다(ADR-002)이며 토큰은 저장소에 남기지 않는다.
- 수치를 만들 데이터가 없을 때 추정·가짜 값을 채우지 마라. 이유: 결과 문서는 실측만 담으며 없으면 '미실행'으로 표시한다(FP-044).
- M5 산출물(`m5_*`)을 재계산하거나 덮어쓰지 마라. 이유: 규칙 16.
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
