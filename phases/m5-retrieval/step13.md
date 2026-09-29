# Step 13: analysis

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/eval/m5_runner.py`(`QuestionRun`, `RunConfig`), `src/kodocs/eval/m5_metrics.py`, `src/kodocs/eval/m5_stats.py`, `src/kodocs/eval/m5_inputs.py`
- `docs/ADR.md` ADR-028 전체, `docs/results/hypotheses.md`(가설표·M5 판정 규칙 절)
- `data/manifest/m5_prior_versions.json` — 개정 전 5건(현행 serial ↔ 직전 serial)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

검색 결과 지표를 사다리·제거·전체 조합·가설 판정·보조 분석으로 만드는 **순수 함수**를 구현한다(입출력은 dict/dataclass, 네트워크·OpenSearch·파일 IO 없음). 신규 파일: `src/kodocs/eval/m5_analysis.py`, `tests/eval/test_m5_analysis.py`.

입력은 `dict[RunKey, list[QuestionRun]]`(조합·문항 집합별 러너 결과)와 문항 메타(유형·출처·태그·반복 여부·split)다. 아래 함수는 모두 JSON 직렬화 가능한 dict를 반환한다.

```python
def ladder(runs, qids) -> dict          # 4단계: standard BM25+고정 → nori BM25+고정 → nori hybrid+고정 → nori hybrid+구조
def ablation(runs, qids) -> dict        # 최종(nori hybrid+구조)에서 한 요소씩: 구조→고정, hybrid→BM25, nori→standard
def full_grid(runs, qids) -> dict       # 14조합 표(평균 Recall@10·NDCG@10·구간 겹침 적중률·1위 적중률, n)
def hypotheses(runs, meta, lookup_top1) -> dict   # H1·H2·H3·H4·H7 각각 판정 문자열과 통계
def h2_breakdown(runs, meta) -> dict    # 출처별(실제/합성)·answer_repeated별 기술 통계
def corpus_v2_rule(silver_test_runs, meta) -> dict
def temporal_prior_vs_current(runs, prior_map, questions) -> dict
def parser_compare(runs_by_condition, qids_real19, qids_real_test13) -> dict
def silver_direction(runs, qids) -> dict        # 골드와 같은 표를 실버 test로 계산(방향 확인용)
def lexical_mismatch_subset(runs, meta) -> dict # 기술 통계만
```

### 각 함수의 고정 정의 (ADR-028·hypotheses.md 그대로)

- **사다리·제거**: 각 단계의 Recall@10·NDCG@10 평균과 **이전 단계 대비 증가분의 짝 비교 부트스트랩 95% CI**(`paired_bootstrap`). 제거 실험은 최종 구성 대비 각 요소를 뺀 구성의 차이(짝 비교).
- **H1**: 골드 test 검색 지표 대상 21문항, 고정 청킹, `nori BM25` vs `standard BM25`의 **문서 단위 Recall@10** 짝 비교, 방향 "nori > standard".
- **H2**: 같은 21문항, 고정 청킹, `nori hybrid`(확정 설정) vs `nori BM25`의 **NDCG@10** 짝 비교, 방향 "hybrid > BM25". `lexical_mismatch` 태그 부분집합은 기술 통계만(`lexical_mismatch_subset`), 판정하지 않는다.
- **H3**: 같은 21문항, nori hybrid(확정 설정)에서 구조 청킹 vs 고정 청킹의 **구간 겹침 적중률**(문항별 0/1) 짝 비교, 방향 "구조 > 고정". 정답 구간이 없는 문항(`span_hit is None`)은 **분모에서 빼고 그 문항 id와 수를 결과에 함께 적는다**.
- **H4**: 골드 test identifier 4문항. `lookup_top1[qid]`(식별자 keyword 방식의 1위 적중 여부, step 14가 계산해 넘긴다)와 최종 구성 hybrid 본문 검색의 `top1`을 짝 비교, 방향 "keyword > 본문". 4문항이라 CI가 넓어 사실상 판정 불가임을 결과에 명시한다. 실버 test identifier 문항(15)으로 같은 비교의 **방향만** 기술 통계로 함께 낸다(판정 아님).
- **H7**: 골드 test 중 identifier·semantic·multi_doc 유형만, 최종 구성의 문서 단위 **Recall@10**을 합성(n=8) vs 실제(n=6)로 **독립 표본 부트스트랩**, 방향 "합성 > 실제". 문항 수가 8·6이 아니면 오류로 멈춘다(사전 고정 값). 전체 유형 비교는 기술 통계로만 별도 보고.
- **판정 문자열**: `verdict()`(채택 / 기각(반대 방향) / 판정 보류(차이 입증 못 함))를 그대로 쓴다. 판정 불가 원인(표본 4 등)은 별도 필드 `note`에 적는다.
- **h2_breakdown**: 각 문항의 (hybrid − nori BM25) NDCG@10 차이를 **출처별(real/synthetic)·answer_repeated별(반복/반복 아님)**로 나눠 `describe`(n·평균·sd·중앙값·최소·최대)만 보고한다(판정·CI 없음). 골드 test와 실버 test를 각각 낸다.
- **corpus_v2_rule**: 실버 test 검색 지표 대상 문항에서, 문항별 (hybrid − nori BM25) NDCG@10 차이(H2와 같은 고정 청킹 쌍)를 만든다. **[반복 아님 그룹 − 반복 그룹]** 평균 차이에 `independent_bootstrap`(10,000회)을 적용한다. 규칙: (1) 두 그룹 중 하나라도 **10문항 미만**이면 `decision = "판정 불가(표본 부족) — 코퍼스 v2 미착수, 한계로 기록"`(CI는 참고로 계산 가능하면 함께 적되 판정에 쓰지 않는다), (2) 아니면 CI 하한 ≥ 0.10이면 `"코퍼스 v2 별도 phase 제안"`, 아니면 `"코퍼스 v2 미착수, 한계로만 기록"`. 그룹 크기와 하한 값을 결과에 그대로 남긴다.
- **temporal_prior_vs_current**: 골드 temporal:revision 문항(dev·test 모두, split 표시)마다 최종 구성의 전체 문서 순위에서 현행 문서 순위와 `prior_map`(현행 doc_id → 개정 전 doc_id)의 개정 전 문서 순위를 낸다(없으면 `null`). `prior_above_current` = 개정 전 순위 < 현행 순위(개정 전만 있으면 참, 둘 다 없으면 거짓). 문항별 표와 test 3문항·dev 2문항의 "개정 전이 위" 개수를 기술 통계로 보고한다(판정 아님).
- **parser_compare**: 조건 3개(`pypdf`, `upstage`, `composite`)의 최종 설정(구조 청킹·확정 hybrid) 결과를 실제 골드 검색 지표 대상 19문항과 test 13 부분집합에서 각각 Recall@10·NDCG@10 평균·기술 통계와 **조건 쌍 3개(composite−pypdf, composite−upstage, pypdf−upstage)의 짝 비교 부트스트랩 CI**로 낸다. 결과 객체에 `"label": "탐색적 비교(사전 등록 가설 아님)"`를 넣는다. 문항 수가 19·13이 아니면 오류.
- **silver_direction**: 실버 test 65문항에 사다리·H1·H2·H3 비교를 같은 정의로 계산하되 "방향 확인용"으로 표시한다. `temporal:revision`·`temporal:as_of_event` 태그별 결과를 분리한다.

### 테스트

각 함수를 손으로 만든 소형 `QuestionRun` fixture(문항 수 몇 개)로 검증한다: 사다리 증가분 계산, H3에서 구간 없는 문항이 분모에서 빠지고 목록에 남는지, H7이 8·6이 아니면 오류인지, `corpus_v2_rule`의 세 갈래(그룹 9문항 → 판정 불가, 하한 ≥ 0.10 → 제안, 그 미만 → 미착수), 그룹 크기 정확히 10인 경계, `temporal_prior_vs_current`의 순위 없음 처리, `parser_compare` 결과 라벨. 실제 데이터·파일 IO에 의존하지 않는다.

## Acceptance Criteria

```bash
make verify
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
   - H1~H7 정의가 ADR-028 9번과 문구 수준까지 같은가(측정 청킹·대조군·방향)?
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
- 판정 방향·대조군·측정 조건(H1·H2는 고정 청킹, H3은 nori hybrid, H4 대조군은 최종 구성 hybrid, H7은 최종 구성)을 구현 중에 바꾸지 마라. 이유: 결과를 보기 전에 고정된 사전 등록 값이다(ADR-026·028).
- 구간 없는 문항을 H3에서 0점으로 넣거나 조용히 빼지 마라. 이유: 분모에서 빼되 목록과 수를 함께 보고하지 않으면 지표가 왜곡된다(FP-021).
- 코퍼스 v2 규칙에 "10문항 미만 → 판정 불가" 조건을 빠뜨리거나, CI만 보고 제안하지 마라. 이유: 표본 부족 그룹의 부트스트랩 하한은 신뢰할 수 없다(사용자 확정 조건).
- `parser_compare`를 가설 판정 표에 섞지 마라. 이유: 사전 등록 가설이 아닌 탐색적 비교다. 판정 문자열(채택/기각)을 붙이지 않는다.
- 이 모듈에서 파일·네트워크·OpenSearch에 접근하지 마라. 이유: 순수 함수여야 단위 테스트로 판정 로직을 검증할 수 있다.
