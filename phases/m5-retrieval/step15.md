# Step 15: report-wiring

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/eval/m4_report.py`, `src/kodocs/eval/templates/`(m3·m4 템플릿) — 고정 결과 파일·`<!-- BEGIN generated -->`/`<!-- END generated -->` 갱신 패턴
- `Makefile`, `AGENTS.md`(명령어 절), `tests/test_infra_compose.py` — Makefile 타깃 검증 방식 참고
- `docs/EVAL.md` 결과 보고 절, `docs/results/hypotheses.md`, `docs/results/2026-09-28-m4-golden-set.md` — 한계 문구
- 이 phase의 모든 CLI: `kodocs.parsing.annex_mapping`, `kodocs.chunking.chunkers`, `kodocs.chunking.corpus --check`, `kodocs.eval.m5_inputs`, `kodocs.retrieval.index_build`, `kodocs.eval.m5_dev`, `kodocs.eval.m5_test`

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

M5 결과 문서 생성기와 초안, make 타깃, AGENTS.md 명령어를 만든다. 신규 파일: `src/kodocs/eval/m5_report.py`, `src/kodocs/eval/templates/m5_retrieval.md`, `docs/results/2026-09-30-m5-retrieval.md`(초안), `tests/eval/test_m5_report.py`, `tests/test_m5_make_targets.py`.

### 1. 결과 문서 `docs/results/2026-09-30-m5-retrieval.md`

M4처럼 **파일 이름을 고정**(실행일과 무관)하고, 생성 영역(`<!-- BEGIN generated -->`~`<!-- END generated -->`)과 사람이 쓰는 영역을 분리한다. 갱신 시 생성 영역 밖은 보존한다. `python -m kodocs.eval.m5_report`는 `m5_config.json`, `m5_dev.json`, `m5_index.json`, `m5_annex_mapping.json`, `m5_eval_inputs.json`, `m5_test_runs.jsonl`, `m5_test_results/run-*.json`을 읽어 채운다. **결과 파일이 없으면 해당 칸을 `(실행 후 기록)`으로 두고 오류 없이 초안을 만든다.** 이 step에서 CLI를 로컬에서 한 번 실행해 초안을 생성한다.

문서 구성(생성 영역 안):
1. 실행 정보: 실행일(Asia/Seoul), 코드 커밋 해시, 측정 환경(플랫폼, 임베딩 장치·모델 리비전·설정 해시, OpenSearch 3.8.0 힙 2g, `init_s`), 고정 조건(청킹 크기·겹침, 확정 hybrid 설정, `config_hash`, 부트스트랩 시드·횟수), 문항 수와 세는 기준.
2. 코퍼스·인덱스 요약: 인덱스 4개의 문서·청크 수, 별표 매핑 결과(매핑 실패·fallback 수 — `m5_annex_mapping.json`), 개정 전 5건 목록, 색인 실패 수.
3. **주 표(사다리)**와 증가분·CI, 보조 표(제거 실험), 부록 전체 조합표(명목 16, kNN 두 행 합쳐 실행 14). 골드 test 21과 실버 test 65(방향 확인용)를 분리해 싣는다. 실제/합성 문항 점수를 분리한 표.
4. 가설 판정: H1·H2·H3·H4·H7 각각 채택/기각(반대 방향)/판정 보류와 원인(표본 크기·CI). H5·H6은 M6·M7 검증 대상이라고 표시. 판정 문항 수 고정값(21, 4, 합성 8·실제 6)을 함께 적는다.
5. 보조 분석(판정 아님): H2의 출처별·answer_repeated별 기술 통계, 어휘 불일치 부분집합, 코퍼스 v2 착수 규칙 결과(그룹 크기·CI·결정), temporal 문항의 개정 전/현행 순위 통계, 파서 탐색 비교(**"탐색적 비교"** 표시, 3조건 기술 통계 + 짝 비교 CI).
6. **첫 실행과 재실행**: 실행 기록(seq·시각·사유·이전 결과 해시)과 각 실행의 핵심 결과를 모두 싣는다.
7. dev 튜닝: 그리드 표(18설정)와 선택 결과.

생성 영역 밖(사람이 쓰는 영역, 초안에 미리 적는다):
- **범위와 제외 사유**: n-gram 분석기 제외 — 사전 등록 가설 어디에도 걸려 있지 않아 판정에 필요 없다.
- **실행 순서(사용자 실행)**: 아래 3절의 순서를 그대로.
- **사전 등록 요약**: ADR-028·hypotheses.md의 M5 판정 규칙 요약과 링크.
- **한계**: 합성 문항이 실제보다 쉽다(M4 한계와 같은 문구), 합성 문서의 서술형 사실 표현 반복이 BM25에 유리할 수 있다(코퍼스 v2 규칙과 연결), 개정 전 5건은 법제처 연혁 텍스트라 현행 문서(파싱본)와 형식이 달라 순위가 치우칠 수 있다, 조합본은 pypdf·업스테이지 혼합이라 어느 단일 파서의 결과가 아니다, 검색 지표 대상 표본이 작아 CI가 넓다(H4는 4문항), 별표 fallback 건, 유형별 수치는 참고용.
- 변경 기록 표.

### 2. make 타깃 (Makefile `.PHONY`와 `AGENTS.md` 명령어 절에 모두 추가)

```
m5-annex-map      # 업스테이지 표 → 별표 id 매핑(로컬, 네트워크 없음) → data/manifest/m5_annex_mapping.json
m5-chunking       # 코퍼스 구조 청킹 통계·고정 청크 크기 확정 → data/manifest/m5_index.json (네트워크 없음)
m5-eval-inputs    # 검색 지표 대상·권한 전제 검사·구간 매핑·answer_repeated → data/manifest/m5_eval_inputs.json (네트워크 없음)
embed-models      # BGE-M3 모델 다운로드(약 2.3GB) — 네트워크, 사용자 실행, `embed` 그룹 설치 후
m5-index-dry      # 색인 청크 수·예상 임베딩 수만 출력 — 네트워크·OpenSearch 없음
m5-index          # 인덱스 4개 빌드(BGE-M3 임베딩·OpenSearch 적재) — make up 필요, 시간이 오래 걸림, 사용자 실행
m5-dev            # dev 튜닝 그리드 → data/manifest/m5_dev.json — 사용자 실행
m5-freeze         # 선택 설정을 data/manifest/m5_config.json에 고정(덮어쓰기 거부) — 사용자 실행
m5-test           # test 1회 실행(재실행은 RERUN_REASON="사유" make m5-test) — 사용자 실행
m5-report         # M5 결과 문서 docs/results/2026-09-30-m5-retrieval.md(고정) 갱신 (네트워크 없음)
```

- `embed-models`: `uv run --group embed python -c "…"`로 `huggingface_hub.snapshot_download("BAAI/bge-m3", allow_patterns=[…필요한 가중치·토크나이저·설정 파일만…])`를 호출해 로컬 경로와 커밋 해시를 출력한다. **정의만 만들고 실행하지 마라.** 의존성 그룹 `embed`는 사용자가 승인 후 `pyproject.toml`에 추가한다는 점을 타깃 주석에 적는다.
- `m5-test`는 `RERUN_REASON`이 설정되면 `--rerun-reason "$(RERUN_REASON)"`를 넘긴다.
- **`make verify`에는 이 타깃들을 넣지 않는다**(네트워크·OpenSearch 필요). `make test-integration`은 이미 있으므로 M5 통합 테스트가 그것으로 돈다.
- 테스트(`tests/test_m5_make_targets.py`): 위 10개 타깃이 `.PHONY`와 본문에 있고 `AGENTS.md`에도 적혀 있는지, `m5-test`가 `RERUN_REASON`을 넘기는지, `verify` 타깃이 위 타깃을 호출하지 않는지.

### 3. 실행 순서(결과 문서 초안에 적을 내용)

1. (사용자, 승인 후) `uv add --group embed sentence-transformers` 등으로 `embed` 그룹 설치, `make embed-models`(약 2.3GB).
2. `make up` (OpenSearch 3.8.0 기동, 이미 빌드된 이미지가 있으면 재사용).
3. 로컬 산출물이 최신인지 확인: `make m5-annex-map`, `make m5-chunking`, `make m5-eval-inputs`(step 실행 중 이미 생성됨, 데이터가 바뀐 경우만 재실행).
4. `make m5-index-dry` → `make m5-index`(인덱스 4개).
5. `make test-integration`(M5 통합 테스트).
6. `make m5-dev` → 결과 확인 → `make m5-freeze`.
7. `make m5-test`(첫 실행) → `make m5-report`. 재실행이 필요하면 `RERUN_REASON="…" make m5-test` 후 `make m5-report`.

### 4. 테스트

`m5_report`: 결과 파일이 없을 때 초안이 `(실행 후 기록)`으로 생성되는지, 결과 fixture가 있을 때 사다리·판정 표가 채워지는지, 생성 영역 밖 텍스트(사람이 쓴 한계·변경 기록)가 갱신 시 보존되는지, 첫 실행과 재실행이 모두 표시되는지, 가설 판정 표에 H5·H6이 "M6·M7 대상"으로 표시되는지, 파서 탐색 비교에 "탐색적 비교" 라벨이 있는지.

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.eval.m5_report
test -f docs/results/2026-09-30-m5-retrieval.md
grep -q "m5-test" AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
   - 결과 초안에 첫 실행/재실행 절, n-gram 제외 사유, 실행 순서, 한계가 모두 있는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 네트워크 요청·다운로드·패키지 설치를 하지 마라. 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 필요해 보이면 `blocked`로 보고한다.
- LLM·외부 API를 호출하거나 `anthropic`·`openai`·`google.genai`·`kodocs.llm`을 import하지 마라. 이유: M5는 결정론 검색 실험 phase이며 LLM 호출이 생기면 멈추고 보고하는 조건이다.
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `src/kodocs/retrieval/`에서 `langchain`·`langgraph`·`kodocs.agent`·`kodocs.mcp_server`·`kodocs.ui`를 (상대 import 포함) import하지 마라. 이유: 검색은 측정 대상이라 추상화 없이 OpenSearch·SQLite를 직접 호출한다(ADR-009).
- 기존 테스트를 깨뜨리지 마라
- `embed-models`·`m5-index`·`m5-test` 등 네트워크·OpenSearch·대용량 실행 타깃을 `make verify`에 넣거나 이 step에서 실행하지 마라. 이유: `make verify`는 API 키·네트워크·외부 서비스 없이 통과해야 하고, 실측은 사용자가 실행한다(AGENTS.md CRITICAL).
- 결과 문서에 수치를 손으로 채우거나 초안에 임의의 예시 수치를 넣지 마라. 이유: 결과 파일이 없으면 `(실행 후 기록)`이어야 한다. 가짜 수치가 실측처럼 남는다.
- 생성 영역 밖의 사람이 쓴 절(한계·변경 기록)을 갱신 때 덮어쓰지 마라. 이유: 검수·해석은 사람이 쓴다(M4 결과 문서와 같은 규칙).
- 파서 탐색 비교에 채택/기각 판정을 붙이지 마라. 이유: 사전 등록 가설이 아닌 탐색적 비교다.
- 결과 문서에 코퍼스·조문 원문을 인용하지 마라(id·해시·수치만).
