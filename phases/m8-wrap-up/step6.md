# Step 6: rerank-core

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `src/kodocs/retrieval/embedding.py` — BGE-M3 상수(`BGE_M3_MODEL_ID`·`BGE_M3_REVISION`·`BGE_M3_ALLOW_PATTERNS`·`BGE_M3_IGNORE_PATTERNS`)와 `resolve_local_snapshot`, 로컬 스냅샷 조회·장치 선택 방식
- `tests/retrieval/test_embedding.py`·`tests/test_m5_make_targets.py` — 모델 상수·오프라인 스냅샷·Makefile 타깃 테스트 방식(FP-045)
- `Makefile`의 `embed-models` 타깃과 주석(다운로드는 만들되 설치는 사용자 형식)
- `src/kodocs/retrieval/search.py`(`ChunkHit` 필드, `SearchParams`), `ordering.py`, `access.py`
- `docs/results/hypotheses.md` 'M8 판정 규칙'(M8-R2·R3·R4·T1)과 `src/kodocs/eval/m8_rules.py`(step 0)
- `tests/test_architecture.py` — `retrieval` 계층 규칙
- **설치된 sentence-transformers 6.1.0 패키지 소스**: `uv run --group embed python -c "import sentence_transformers, os; print(os.path.dirname(sentence_transformers.__file__))"`로 경로를 얻어 `CrossEncoder` 정의(생성자 인자, `predict` 시그니처, `rank`, 기본 활성화 함수, 토크나이저 호출 방식)를 **직접 읽는다**. 이 소스를 읽지 않고 쓴 시그니처는 금지다.
- 설치된 `torch`의 MPS 관련 API(장치 가용성, 메모리 조회) — 설치된 패키지 소스·docstring을 읽어 확인한다

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 이 phase(m8-wrap-up) 개요

PRD M8: 공개 가능한 마무리(공개 전환 점검, ADR 정리, Streamlit GUI, README)와 리랭커 실험(H8). 속도 우선이라 필수 항목(step 0~5)을 먼저 끝내 언제든 공개할 수 있는 상태를 만들고, 리랭커(step 6~8)는 그 뒤에 붙인다.

| step | name | 내용 |
|---|---|---|
| 0 | `m8-prereg` | H8 사전 등록(hypotheses.md 'M8 판정 규칙', ADR-031)과 규칙 절 해시 모듈 |
| 1 | `public-audit` | 공개 전환 점검 스캐너(`make m8-audit`·`make m8-audit-gh`)와 점검 문서 |
| 2 | `docs-cleanup` | ADR 상태·색인, M7 정정(패키지명·parse_error 오프라인 분류), 별표 한계를 M8 결과 문서로 |
| 3 | `streamlit-ui` | Streamlit 데모 GUI(질의 화면·결과표 뷰어), answer()만 호출 |
| 4 | `readme` | README 생성기(`make readme`)와 README.md. H8은 결과가 없으면 '미실행' |
| 5 | `ui-streaming` | parse_error 재분류(원장 근거), 최종 답변 실제 스트리밍·도구 호출 실시간 표시, 폭주 재현 실행 코드(`make m8-stream-repro`) |
| 6 | `rerank-core` | 리랭커 상수·CrossEncoder 래퍼·순서 재배열 순수 함수·`make rerank-models` |
| 7 | `rerank-dev` | 후보·점수 캐시, max_length 확정, dev 격자, freeze |
| 8 | `rerank-test` | test 1회 잠금, 대조군 일치 검사, 짝 비교 부트스트랩, 지연, 결과 문서 |

고정 전제(모든 step 공통):
- M5~M7 확정 결과와 고정 설정(m5 `config_hash` `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`, `data/manifest/m6_config.json`, `data/manifest/m7_config.json`)은 소급 변경하지 않는다. M8 실험은 새 구성으로 따로 기록하고 기존 판정과 섞지 않는다.
- M8 결과 문서 파일명 날짜는 고정값 `2026-10-03`이다: `docs/results/2026-10-03-m8-wrap-up.md`(step 2), `2026-10-03-m8-public-readiness.md`(step 1), `2026-10-03-m8-rerank.md`(step 8).
- 이 phase의 step에서는 실제 API 호출, 모델·데이터 다운로드, 패키지 설치, `.env` 열람을 하지 않는다. 사용자가 직접 실행하는 명령은 step 파일이 아니라 사용자가 실행한다.

## 작업

리랭커 실험에 쓸 **재사용 부품**을 만든다: 모델 상수, CrossEncoder 래퍼, 순서 재배열 순수 함수, 토큰 길이 측정, `make rerank-models`. 실제 모델 실행·점수 계산·다운로드는 하지 않는다(사용자가 `make rerank-models`를 실행한다).

### 1. 상수 한 곳 (`src/kodocs/retrieval/rerank.py`)
다음을 **이 파일 한 곳에** 정의하고 Makefile·테스트·다른 모듈이 import한다(FP-045: 다운로드와 조회가 같은 상수를 써야 한다).
- `RERANKER_MODEL_ID = "BAAI/bge-reranker-v2-m3"`, `RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"`, `RERANKER_LICENSE = "Apache-2.0"`(fp32 `model.safetensors` 2,271,071,852바이트).
- `RERANKER_EXPECTED_FILES`: 필수 파일 6개의 이름을 모두 넣는다 — `config.json`, `model.safetensors`, `sentencepiece.bpe.model`, `special_tokens_map.json`, `tokenizer.json`, `tokenizer_config.json`. 바이트 수는 `model.safetensors`만 `2271071852`로 검사하고 나머지는 존재 여부만 본다.
- `RERANKER_ALLOW_PATTERNS = ("*.json", "*.model", "*.safetensors")`, `RERANKER_IGNORE_PATTERNS = ("assets/*", "*.png", "*.md")`. 코드 주석에 근거를 그대로 쓴다: "2026-10-02 HF API로 revision 953dc6f6… 파일 목록 확인". 확인된 목록: `.gitattributes`, `README.md`, `assets/*.png` 4개, `config.json`, `model.safetensors`, `sentencepiece.bpe.model`, `special_tokens_map.json`, `tokenizer.json`, `tokenizer_config.json`이며 `onnx` 폴더는 없다. 이 목록은 사용자가 확인한 값이므로 이 step에서 네트워크로 다시 확인하지 않는다.
- 테스트: 상수가 `docs/results/hypotheses.md` 'M8 판정 규칙'에 적힌 revision·바이트 수와 같다(규칙 절 텍스트에서 파싱해 비교, M8-R2) — 다르면 실패.

### 2. `make rerank-models`와 오프라인 스냅샷 검증
- `resolve_local_reranker_snapshot(cache_dir=None) -> Path`: `snapshot_download(RERANKER_MODEL_ID, revision=RERANKER_REVISION, allow_patterns=..., ignore_patterns=..., local_files_only=True)`로 조회한다(다운로드와 **같은 상수**). 실패 시 원래 예외를 메시지에 함께 보인다(FP-045). 조회 후 **필수 파일 검증**: `RERANKER_EXPECTED_FILES`의 6개 파일이 모두 존재하고 `model.safetensors`의 바이트 수가 `2271071852`와 같다, 설치된 `transformers`의 `AutoTokenizer.from_pretrained(path, local_files_only=True)`와 `AutoConfig.from_pretrained(path, local_files_only=True)`가 오프라인으로 로드된다(로딩 방법은 설치된 패키지로 확인). 어긋나면 어떤 파일이 없는지·바이트가 얼마인지를 담은 오류를 낸다. 패턴·필수 파일 목록은 실제 파일 목록으로 확인된 값이지만 오프라인 검증은 생략하지 않는다(FP-045).
- Makefile: `.PHONY`에 `rerank-models`를 추가하고 `embed-models`와 같은 형식으로 `rerank-models:`를 만든다(`snapshot_download` 호출에 위 상수를 import, 다운로드 후 `resolve_local_reranker_snapshot`으로 검증하고 경로·revision·모델 파일 바이트 수를 출력). 주석에 "약 2.3GB급 모델만 내려받는다. 사용자 실행"을 쓴다. 이 타깃을 이 step에서 **실행하지 않는다**.
- 테스트(`tests/test_m8_rerank_make_targets.py`·`tests/retrieval/test_rerank.py`): 허용·제외 패턴과 필수 파일 6개 이름이 위 값과 같다 / `test_rerank_models_target_is_declared_but_not_run_by_verify`(FP-045의 `test_embed_models_is_declared_but_not_installed_by_make` 방식), 스냅샷 조회가 revision·패턴을 오프라인으로 고정하는지(fake `snapshot_download`), 실패 메시지에 원래 원인이 남는지, 바이트 수 불일치·필수 파일 6개 중 하나라도 누락 시 오류(없는 파일 이름을 메시지에 포함).

### 3. CrossEncoder 래퍼 (`rerank.py`)
- **먼저 설치된 sentence-transformers 소스를 읽고** 다음을 확인해 코드 주석·테스트에 근거 줄(파일:줄)과 함께 남긴다(FP-011): 생성자의 인자(모델 경로, `max_length`, `device`, 추가 로딩 인자), `predict`의 시그니처(문장쌍 입력 형식, `batch_size`, `show_progress_bar`, 활성화 함수 인자, 반환 타입), 단일 라벨 모델의 기본 활성화 함수, 토크나이저 호출 방식(문장쌍 인코딩·`truncation` 정책). 기억과 다르면 소스 기준으로 쓴다.
- `RerankCandidate(chunk_id: str, doc_id: str, text: str, base_rank: int)`(frozen dataclass; `base_rank`는 대조군 순위로 1부터).
- `class CrossEncoderReranker`: 생성자 `(model_path: Path, *, max_length: int, device: str, batch_size: int, model_factory: Callable | None = None)`. `model_factory`는 테스트가 가짜 모델을 주입하는 용도다. `score(question: str, texts: Sequence[str]) -> list[float]`는 (질문, 텍스트) 쌍을 점수화한다. 점수는 순위에만 쓴다(M8-R3).
- **장치 정책**: `device`는 명시한다. `"mps"`를 요청했는데 사용할 수 없으면 CPU로 **조용히 내려가지 않고** 오류를 낸다. 실제 사용된 장치·batch_size·`max_length`·모델 revision을 `describe()`로 돌려준다(결과에 기록하기 위해, FP-044). 모델 로딩 시간은 외부에서 `init_s`로 따로 잴 수 있게 로딩과 점수를 분리한다(FP-036).
- `mps_memory_snapshot() -> dict`: 설치된 torch API로 확인한 방법으로 현재 MPS 할당 메모리 등을 읽는다. MPS가 아니거나 API가 없으면 필드를 `None`으로 둔다. 기록 전용이며 판정에 쓰지 않는다(M8-P1). 사용한 torch 함수 이름은 소스를 읽고 확인한 것만 쓴다.
- `measure_pair_lengths(tokenizer, question, texts) -> list[int]`: 토크나이저로 (질문, 텍스트) **쌍을 특수 토큰 포함, `truncation=False`로** 인코딩한 토큰 길이를 돌려준다(M8-R4: 실제 입력 쌍 길이). 인코딩 방식이 `CrossEncoder`의 실제 쌍 인코딩과 같은지 소스로 확인한다. `p99_nearest_rank(values) -> int`(오름차순 `ceil(0.99n)`번째)와 `choose_max_length(lengths, cap=1024) -> MaxLengthChoice(value, p99, capped: bool, truncated_pairs, total_pairs)`를 둔다: P99 이상인 가장 작은 2의 거듭제곱, 상한 `cap`, `cap`을 넘으면 `cap`을 쓰고 잘린 쌍 수·비율을 계산한다.

### 4. 순서 재배열 순수 함수 (`rerank.py`)
- `reorder(candidates: Sequence[RerankCandidate], scores: Sequence[float], *, depth: int, fusion: Literal["alone", "rrf"]) -> list[str]`(chunk_id 순서). 규칙은 M8-T1 그대로다: `alone`은 상위 `depth`개를 점수 내림차순(동점이면 `base_rank` 오름차순)으로 재배열하고 depth 밖은 대조군 순서로 뒤에 둔다. `rrf`는 depth 안의 후보에 `1/(60 + base_rank) + 1/(60 + 리랭커 순위)`(리랭커 순위는 depth 안에서 1부터, 점수 내림차순·동점은 base_rank)를 주어 내림차순(동점이면 base_rank)으로 재배열하고 depth 밖은 같다. `rank_constant=60`은 상수로 둔다. `scores`는 `candidates`와 길이가 같아야 하며 `depth`는 `1..len(candidates)`다(아니면 `ValueError`). 입력 순서를 가정하지 말고 `base_rank`로 정렬해 쓴다.
- 이 모듈은 OpenSearch·사용자 컨텍스트를 받지 않는다. 입력은 `authorized_*`가 이미 돌려준 후보뿐이다(M8-R1). 권한 로직을 만들지 않는다.
- 테스트: `alone`·`rrf` 각각의 순서(손으로 계산한 작은 예, 동점 규칙 포함), depth 밖 후보 보존, 잘못된 depth·길이 불일치 오류, 재배열이 후보 집합을 바꾸지 않음(순열), `choose_max_length`의 경계(P99가 정확히 2의 거듭제곱일 때, 1024 초과 시 상한과 잘린 비율, 아주 작은 길이), `measure_pair_lengths`가 fake 토크나이저에서 truncation을 끄고 쌍 인코딩을 호출한다, 장치 정책(mps 불가 시 오류, cpu 명시는 허용), 가짜 `model_factory`로 `score`가 쌍 입력과 `batch_size`를 소스에서 확인한 호출 형식대로 넘긴다.
- 계층 테스트: `rerank.py`가 `langchain*`·`langgraph*`·`kodocs.tools`·`kodocs.agent`·`kodocs.mcp_server`·`kodocs.ui`를 import하지 않는다(`tests/test_architecture.py`가 이미 강제하는지 확인하고, 이 파일이 검사 대상에 포함되는지 확인한다).

### 5. 문서·명령
- `AGENTS.md`의 아키텍처 규칙에 한 줄을 추가한다: "`retrieval/rerank.py`는 `authorized_*`가 돌려준 후보만 입력으로 받고 새 검색·권한 로직을 만들지 않는다(M8-R1)." 그리고 `## 명령어`에 `make rerank-models  # bge-reranker-v2-m3 다운로드(revision 고정, 약 2.3GB급) — 네트워크, 사용자 실행, embed 그룹 설치 후`를 추가한다.

## Acceptance Criteria

```bash
make verify   # ruff + pytest 통과
make -n rerank-models   # 타깃이 선언돼 있는지만 확인(실행하지 않는다)
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조와 계층 경계(`retrieval`은 langchain·langgraph·tools·agent·mcp_server·ui를 import하지 않는다 등)를 따르는가?
   - ADR 기술 스택을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙을 위반하지 않았는가?
   - 이 step의 `## 금지사항`을 모두 지켰는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(생성·수정 파일, 핵심 결정, 다음 step이 알아야 할 값)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 수동 설정, 코드에서 확인할 수 없는 값 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `make rerank-models`를 실행하거나 모델·토크나이저를 다운로드하지 마라. `huggingface_hub`·`transformers`로 네트워크 조회도 하지 마라. 이유: 다운로드는 사용자가 실행한다(FP-040, ADR-014). 테스트는 fake로만 한다.
- sentence-transformers의 `CrossEncoder`·`predict` 시그니처를 기억으로 쓰지 마라. 설치된 패키지 소스를 읽어 확인하고 근거 줄을 남겨라. 이유: FP-011(외부 라이브러리 계약 추측). 소스와 기억이 다르면 소스가 맞다.
- 허용·제외 패턴과 필수 파일 목록을 임의로 늘리거나 바꾸지 마라. 이유: FP-045(부분 다운로드 스냅샷은 패턴이 어긋나면 불완전으로 판정된다). 값은 2026-10-02 HF API로 revision 953dc6f6… 파일 목록을 사용자가 확인한 것이다. 바꿔야 하면 `blocked`로 보고한다.
- `mps`를 요청했는데 CPU로 조용히 내려가는 코드를 만들지 마라. 이유: 지연 측정 환경이 달라진 채 결과가 기록된다(FP-044). 장치는 명시하고 실제 값을 기록한다.
- 모델 로딩 시간을 점수 계산 시간에 섞지 마라. 이유: FP-036(1회성 초기화가 문서별 시간에 섞임).
- `rerank.py`에 사용자 컨텍스트·권한 로직·OpenSearch 호출을 넣지 마라. `tools`·`search_docs`에 연결하지 마라. 이유: 권한은 `retrieval.authorized_*` 한 곳에 있고(AGENTS.md CRITICAL), 리랭커는 그 결과만 입력으로 받는다. M6 도구 계약(`search_docs` v1)은 바꾸지 않는다(M8-R1).
- 점수 절대값으로 임계값을 만들거나 후보를 버리지 마라. 이유: 점수는 순위에만 쓴다(M8-R3). depth 밖 후보도 대조군 순서로 유지한다.
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 5·7·8이 만드는 코드가 **사용자 실행 시** 쓰는 `data/interim/m8/`(재현 원시 출력·후보·점수 캐시)만 허용되며 이 경로도 커밋하지 않는다. 에이전트가 step 안에서 이 경로에 쓰지는 않는다(실행은 사용자).
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
