# Step 1: m5-carryover

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `docs/results/2026-09-30-m5-retrieval.md` — 'M6로 넘기는 결정' (c)와 한계 절
- `src/kodocs/retrieval/embedding.py` — `get_sentence_embedding_dimension` 호출부(271행 부근)와 관련 테스트 `tests/retrieval/test_embedding.py`
- `src/kodocs/eval/templates/m5_retrieval.md` — '한계' 절
- `src/kodocs/eval/m5_report.py`와 관련 테스트(`tests/eval/`)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-029)과 판정 규칙(hypotheses.md의 'M6 판정 규칙')은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)으로 고정이며 바꾸지 않는다.

## 작업

M5 이월 두 가지만 처리한다. 다른 파일은 건드리지 않는다.

### 1. BGE-M3 임베딩 차원 FutureWarning 정리
- `retrieval/embedding.py`에서 `model.get_sentence_embedding_dimension()`이 FutureWarning을 낸다. 설치된 `sentence-transformers`에서 새 메서드 이름과 경고 내용을 **실제로 확인**한다(`uv run python -c ...`로 설치본의 소스·`help`를 읽는다. `embed` 그룹이 설치되어 있지 않으면 확인 불가로 보고하고 아래 호환 방식만 구현한다. 메서드 이름을 추측하지 마라 — FP-011).
- 새 이름이 있으면 그것을 우선 쓰고 없으면 옛 이름으로 폴백한다(둘 다 없으면 기존과 같은 오류). 반환 차원 검증 동작(1024 등)은 그대로다.
- 테스트: 새 이름만 있는 fake 모델, 옛 이름만 있는 fake 모델, 둘 다 있는 fake 모델 각각에서 올바른 차원이 나오고 **옛 이름이 호출되지 않거나(새 이름 있을 때) 경고 없이 동작**함을 `warnings.simplefilter("error")`로 검증한다.

### 2. `templates/m5_retrieval.md`의 옛 한계 문구 삭제
- 템플릿 '한계' 절의 `- 합성 문항은 실제 문항보다 쉽다.` 줄을 삭제한다(H7이 반대 방향으로 기각되어 검색 지표에서 성립하지 않는 전제다).
- 테스트: 템플릿 파일에 `합성 문항은 실제 문항보다 쉽다`가 없어야 한다. 또한 `docs/results/2026-09-30-m5-retrieval.md`의 수치·본문은 이 step에서 바뀌지 않아야 한다(파일을 수정하지 마라).

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - M5 결과 문서와 산출물이 바뀌지 않았는가(`git diff --stat`에 docs/results·data/manifest가 없는가)?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 새 임베딩 메서드 이름을 문서·기억으로 추측해 하드코딩하지 마라. 이유: 외부 라이브러리 API는 설치된 실제 버전으로 확인한다(FP-011).
- 임베딩 모델 다운로드·`make embed-models`를 실행하지 마라. 이유: 약 2.3GB 네트워크 작업이며 사용자 실행 대상이다.
- `docs/results/2026-09-30-m5-retrieval.md`를 수정하지 마라. 이유: M5 결과 문서는 이미 정리되어 있고 수치가 바뀌면 안 된다.
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
