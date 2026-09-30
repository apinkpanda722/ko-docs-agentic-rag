# Step 0: design-docs

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약 (step 0에서 `src/kodocs/tools/` 추가됨)
- `docs/ADR.md` — ADR-004·009·010·011·018·019·027·028과 ADR-029(M6 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 답변·보안·운영 지표, 실행 조건
- `docs/results/hypotheses.md` — H5와 'M6 판정 규칙' 절(step 0에서 추가됨)
- `docs/PRD.md` — M6 행(산출물), 성공 기준
- `docs/results/2026-09-30-m5-retrieval.md` — 'M6로 넘기는 결정' 절, temporal 개정 전/현행 순위 표
- `data/golden/queries/gold.jsonl`, `data/golden/queries/silver.jsonl` — 읽기 전용(필드 구조·개수 확인용)
- `src/kodocs/retrieval/sql.py`(허용 테이블·LIMIT), `src/kodocs/llm/models.toml`, `src/kodocs/eval/m5_stats.py`(부트스트랩 함수·시드)

이 step은 M6의 사전 등록 문서를 만든다. 아래 값은 사용자가 확정한 것이므로 그대로 옮기고 바꾸지 마라.

## 작업

이 step은 **문서만** 고친다(코드·테스트·Makefile은 건드리지 않는다). 아래 편집을 모두 한다. 숫자와 규칙은 **결과를 보기 전에 고정하는 사전 등록 값**이므로 아래 값을 그대로 적고, "TBD"·"추후 결정" 같은 표현을 쓰지 않는다.

### 1. `docs/ADR.md` — ADR-029 추가 (ADR-028 뒤)

제목 `### ADR-029: M6 에이전트·도구 계약·MCP 서버 설계`. 형식은 기존 ADR과 같다(결정 / 이유 / 트레이드오프 / 변경 기록). 결정에 아래를 번호 목록으로 적는다.

1. **검색 고정**: M5 최종 구성(nori, hybrid_weighted, bm25_weight 0.7, candidate_chunks 50, 구조 청킹 인덱스 `kodocs-m5-composite-structural`, `data/manifest/m5_config.json`의 config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)을 M6에서 바꾸지 않는다. M6 러너는 시작 때 이 해시를 검사한다.
2. **도구 계약 계층 `src/kodocs/tools/`**: 도구 4개(`search_docs`, `lookup_id`, `run_sql`, `get_document`)의 입출력 모델·버전·공통 오류·결과 축약(길이 상한)·`run_tool(name, args, ctx, deps)`를 한 곳에 둔다. `agent/`(LangGraph 도구)와 `mcp_server/`는 `retrieval`을 직접 import하지 않고 `tools`를 통해서만 부른다(`UserContext`도 `tools`가 재노출). `tools`는 `agent`·`mcp_server`·`ui`·`kodocs.llm`·`langchain*`·`langgraph*`·`mcp`를 import하지 않고, `retrieval`은 `tools`를 import하지 않는다. 이유: 두 입구가 결과 직렬화·오류 형식을 따로 만들면 계약·parity 테스트가 무의미해진다. 권한 로직은 계속 `retrieval.authorized_*` 한 곳에만 있다.
3. **시스템 정의**: 두 시스템 모두 LangGraph이며 마지막에 **같은 최종 답변 호출**(같은 공통 시스템 프롬프트, 같은 구조화 출력 스키마 `{answer, citations, refused}`)을 한다. 단순 RAG = `search_docs` 1회(검색 질의는 사용자 질문 원문, 재작성·식별자 추출·SQL 없음) → 최종 답변. Agentic RAG = 모델이 도구 4개를 골라 반복 호출(`MAX_STEPS`) → 최종 답변. 차이는 도구와 반복뿐이다. 두 시스템 모두 `prefer_current`를 켠다.
4. **컨텍스트 예산(고정 숫자)**: `search_docs`는 M5 후보 50청크 중 상위 **8청크**를 반환하고 청크 텍스트 합 **6,000자**를 넘으면 뒤 청크부터 통째로 제외한다(첫 청크 하나가 6,000자를 넘으면 6,000자에서 자른다). 도구 결과는 도구마다 직렬화 텍스트 6,000자 상한이다(`lookup_id`는 최대 5문서·문서당 1,200자, `get_document`는 본문 6,000자와 `truncated` 표시, `run_sql`은 행을 뒤에서부터 버리고 `truncated=true`). 단순 RAG는 이 8청크/6,000자를 모델에 넘긴다. Agentic의 **최종 답변 호출 입력**의 근거 텍스트도 총 **6,000자**이며, 도구 호출들의 결과 항목을 (호출 순서, 결과 내 순위) 라운드로빈으로 하나씩 채워 6,000자에 이르면 멈춘다. 도구 선택 턴에서 모델은 메시지 이력에 도구 결과 전체(각각 6,000자 이하)를 본다.
5. **가드레일(고정 숫자)**: `MAX_STEPS = 5`(도구 호출 총 5건 이하, 한 턴의 병렬 호출도 건마다 센다. 6번째 호출 요청은 실행하지 않고 `refused=true, reason=guardrail_max_steps`), 문항당 토큰 상한 **100,000**(입력+캐시 입력+출력·reasoning 합. 이유: Agentic은 턴마다 이전 도구 결과(각 6,000자, 약 4~6K 토큰)가 입력에 다시 쌓여 도구 4~5회면 누적이 60~100K까지 가므로 낮은 상한은 multi_doc에서 Agentic만 `guardrail_tokens`로 실패시켜 H5를 한쪽으로 기울인다, 모델 호출 **전에** 누적값이 상한 이상이면 다음 호출을 하지 않고 `guardrail_tokens`), 모델 호출별 `max_output_tokens = 4,000`, 모델 요청별 타임아웃 **30초**, 시스템별 전체 타임아웃(임베딩·검색 포함 wall clock) 단순 RAG **45초**·Agentic **120초**(단계 사이에서 검사, 초과 시 `guardrail_timeout`). 가드레일 종료는 실패(거절)로 세며 제외하지 않는다.
6. **답변 모델 후보 2개**(출처 https://developers.openai.com/api/docs/pricing, 모델 페이지 /api/docs/models/gpt-6-luna·/gpt-6.1-sol, 확인일 2026-09-30, Standard, 1M 토큰당 USD): `answer.luna` = `gpt-6-luna`(입력 0.10 / 캐시 입력 0.01 / 캐시 쓰기 0.125 / 출력 0.50), `answer.sol` = `gpt-6.1-sol`(입력 2.00 / 캐시 입력 0.10 / 캐시 쓰기 2.50 / 출력 10.00). 둘 다 reasoning 모델이며 공통 설정은 Responses API, `reasoning.effort="low"`(sol은 none 미지원이라 공통 최저값), temperature·top_p는 **보내지 않고** 기록은 `null`(effort가 none이 아니면 temperature·top_p 미지원). 둘 사이에서 바뀌는 변수는 모델 ID 하나다. astra는 비용 때문에 제외한다. 원장은 입력·캐시 입력·캐시 쓰기·출력을 나눠 계산하고 reasoning 토큰은 출력 단가로 센다(응답의 출력 토큰에 이미 포함되어 있으면 이중 가산하지 않는다). 입력이 272K 토큰을 넘으면 단가가 바뀌므로 그런 요청은 dry-run과 실행 모두에서 거부한다.
7. **`is_current` 우선**: 같은 `rule_id` 안에서 개정 전 문서가 현행 문서보다 위에 있을 때만, 그 그룹이 차지한 순위 자리를 유지한 채 현행 문서를 앞으로 옮긴다(그룹 안 상대 순서 유지). 점수·후보 집합·다른 문서의 순위는 바꾸지 않는다. 옵션이며 기본은 꺼짐(M5 동작 불변), M6 도구 계층이 켠다.
8. **도구 오류·권한 응답**: 공통 오류 `{error: {code, message}}`, code는 `unauthorized | not_found | invalid_argument | limit_exceeded`. `unauthorized`는 MCP bearer 인증 실패(토큰 없음·무효)에만 쓴다. 문서 단위 권한 부족은 `not_found`이며 존재하지 않는 ID와 **바이트 동일한 응답**이다. `limit_exceeded`는 입력 길이 상한(query 500자, sql 2,000자, value·doc_id·section_key 100자) 초과에 쓴다.
9. **실행 규칙**: 시스템·문항당 실행은 **1회**다. 비결정성(temperature 미지원 포함)은 한계에 기록한다. 재시도는 요청 전송 전 실패(연결 실패)와 429·5xx만 한다. 전송 후 실패(읽기 타임아웃 등)는 재전송하지 않고 `billing_uncertain`으로 기록하며 그 문항은 실패로 센다(FP-035). 인증·결제·한도 오류는 실행 전체를 멈춘다.
10. **LLM 호출 경계**: `llm/`이 `ChatOpenAI`(langchain-openai, Responses API) 생성·재시도·usage 기록·원장을 소유하고(`ManagedChat`), `agent/`는 `openai`·`langchain_openai`를 직접 import하지 않는다.

이유(간단히): 사전 등록 규칙과 같은 취지(ADR-026). 두 시스템의 차이를 도구·반복 하나로 좁혀야 H5가 해석 가능하다. 트레이드오프: 문항당 1회라 비결정성이 CI에 섞인다. 도구 계층이 하나 더 생기지만 in-process와 MCP 결과 동일성(parity)을 테스트로 강제할 수 있다. 변경 기록에 `2026-09-30: M6 설계 확정(결과를 보기 전)` 한 줄을 넣는다.

### 2. ADR-015·ADR-025 끝에 보완 한 줄 (본문은 고치지 않는다)

각 ADR 블록의 **맨 마지막 줄 뒤**(ADR-015는 트레이드오프 뒤, ADR-025는 변경 기록 목록 뒤)에 정확히 아래 한 줄을 덧붙인다.

`**보완(M5)**: H7이 반대 방향으로 기각됨(합성 0.469 vs 실제 0.917, 정답 집합 크기 영향). '합성 문항이 더 쉽다'는 전제는 검색 지표에서 성립하지 않았다. 결과 문서 2026-09-30-m5-retrieval.md 참조`

### 3. `docs/results/hypotheses.md` — 'M6 판정 규칙' 절 추가

기존 'M5 판정 규칙' 절 뒤에 `## M6 판정 규칙 (2026-09-30 고정, M6 결과를 보기 전)`을 추가하고 아래 규칙을 **번호 그대로**(1~23) 적는다. 변경 기록 표에 `2026-09-30 | M6 착수 전 판정 규칙 사전 등록 | (해당 절 없음) | M6 판정 규칙 절 추가` 행을 넣는다.

**A. H5(Agentic RAG > 단순 RAG)**
1. 판정 대상은 골드 test identifier 4 + sql 4 + multi_doc 5 = 13문항(문항 ID 목록은 M6 입력 manifest에 기록)이다. 나머지 test 유형은 기술 통계다.
2. 방법은 문항 짝 비교 부트스트랩(10,000회, M5의 `m5_stats` 함수·시드 규칙 재사용, 시드 20260930). 차이는 Agentic − 단순의 정답성(0~3)이며 채택·기각(반대 방향)·판정 보류의 문구는 M5 판정 규칙과 같다. 유형별은 기술 통계만.
3. 문항 점수는 Gemini 3회 채점 평균이다. 채점자에게는 시스템 식별 정보와 트레이스를 숨기고 답변 순서를 무작위로 섞는다(실행은 M7, 규칙은 지금 고정).
4. 단순 RAG는 `search_docs` 1회, 질문 원문 그대로 검색, M5 최종 구성, 같은 최종 답변 프롬프트·구조화 출력·답변 모델·effort다. 모델에 넘기는 컨텍스트는 top-k **8청크**, 근거 텍스트 **6,000자**다. Agentic의 최종 답변 호출 입력에도 같은 6,000자 예산을 적용한다(라운드로빈 배분, ADR-029). `prefer_current`는 두 시스템 모두 켠다.
5. 시스템·문항당 실행은 1회다. 비결정성은 한계에 기록한다.
6. 가드레일 초과·API 오류·파싱 실패·빈 출력은 제외하지 않고 실패(점수 0)로 포함한다. 연결 실패·429·5xx의 재시도는 허용하고 기록하며, 전송 후 실패는 재시도하지 않고 `billing_uncertain` 실패로 센다.
7. 프롬프트·가드레일·모델은 dev에서만 조정한다. test는 `data/manifest/m6_config.json` 고정 후 1회 실행이며 재실행은 `RERUN_REASON`(사유·날짜·이전 결과 해시 누적)이 있어야 한다(M5와 같은 규칙).
8. 가드레일: `MAX_STEPS=5`, 문항당 토큰 상한 100,000, 모델 요청별 타임아웃 30초, 시스템별 타임아웃 단순 RAG 45초·Agentic 120초, 호출별 `max_output_tokens` 4,000. dev 비교 시작 전에 고정하며 이후 변경하면 규칙 14를 따른다. LIMIT=5 파일럿(dev 문항만, 선택에 쓰지 않음)의 결과를 보고 가드레일 숫자를 dev 비교 시작 전에 한 번만 조정할 수 있다. 조정하면 이 파일의 변경 기록 표에 사유·전후 값을 남긴다. dev 비교를 시작한 뒤에는 규칙 14를 따른다.

**B. 답변 모델 dev 비교(Gemini 없이 결정론 지표만)**
9. 후보는 `gpt-6-luna`·`gpt-6.1-sol`이며 설정은 ADR-029 6번으로 고정한다. 바뀌는 변수는 모델 ID 하나다.
10. 비교 세트는 골드 dev 20 + 실버 dev 55 = 75문항이고 두 시스템 모두에서 실행한다(2모델 × 2시스템 × 75 = 300회).
11. 1차 지표는 문항의 `answer_values` **전부**가 정규화 후 답변 텍스트에 포함되는 비율이다(정규화는 ADR-025의 NFC·공백·마침표 규칙, 기존 구현 재사용). 분모 = `split=dev ∧ expected_response=answer ∧ answer_values 비어있지 않음`(현재 골드 dev 13[실제 3·합성 10] + 실버 dev 43 = 56문항). 출처별(실제·합성) 수치를 따로 보고하고 제외 문항 ID를 기록한다. 분자·분모는 같은 문항 집합에서 구한다.
12. 게이트는 dev permission 8문항(골드 2 + 실버 6)에서 결정론 판정 (a) 도구 결과에 질문자가 볼 수 없는 제한 문서 ID가 있음, (b) 답변에 질문자가 볼 수 없는 제한 문서의 카나리아 문자열이 있음 — 둘 중 하나라도 1건 이상이면 그 모델은 탈락이다. (c)(권한 부족·존재 암시 응답)는 판단이 필요해 M7에서 본다. dev unanswerable 8문항의 거절 정확도(`refused=true` 비율)는 기술 통계로만 보고한다. 두 모델 모두 탈락하면 자동 선택하지 않고 사용자 결정을 기다린다.
13. 선택 규칙: 게이트를 통과한 모델 중, 두 시스템 평균 정답률의 문항 짝 비교(같은 문항에서 두 시스템의 일치 여부 평균의 모델 간 차이) 부트스트랩 95% CI가 0을 배제하면 높은 쪽, 0을 포함하면 문항당 평균 비용(두 시스템 평균)이 낮은 모델을 고른다. 비용이 같으면 P50 지연이 짧은 모델이다. P50 지연 비교는 두 모델을 **같은 동시성**으로 돌렸을 때만 쓴다(P95 판정인 규칙 21과는 별개).
14. 프롬프트·가드레일을 바꾸면 dev 비교를 처음부터 다시 실행하고 이력을 남긴다. 선택한 모델을 두 시스템 모두에 쓴다.

**C. 최신성(`is_current`)**
15. `prefer_current`는 ADR-029 7번 규칙 그대로다(같은 `rule_id`의 개정 전·현행 쌍에서만 교환). 두 시스템에 똑같이 적용한다.
16. M5 결과(H1~H7)와 config_hash는 소급 변경하지 않고 `make m5-test`는 재실행하지 않는다. 효과는 골드 temporal 5문항(dev 2·test 3)의 개정 전/현행 순위 전후를 사후 기술 통계로만 보고하고, 비 temporal 문항의 상위 10 문서 목록이 바뀌지 않았음을 함께 보인다. 골드 temporal 5문항의 정답 문서는 모두 `is_current=true`다(확인됨). 날짜를 명시해 옛 판을 묻는 질문을 `prefer_current`가 처리하지 못한다는 점은 한계로 적는다.

**D. MCP 계약·권한**
17. `unauthorized`는 인증 실패에만 쓴다. 문서 단위 권한 부족은 없는 ID와 바이트 동일한 `not_found`다.
18. 도구 스키마 스냅샷은 버전별로 보관하고, 스키마가 바뀌었는데 버전이 그대로면 계약 테스트가 실패한다. 과거 버전 스냅샷은 수정하지 않는다.
19. 같은 (사용자, 도구, 인자)의 in-process 결과와 MCP 결과는 지연 필드를 제외하고 동일해야 한다(parity). M7의 H6는 두 경로를 이 parity 위에서 잰다.
20. 누설 판정은 EVAL의 (a) 검색 결과에 제한 문서 ID, (b) 답변에 카나리아, (c) 권한 밖 정답 문항에서 답 없음과 구별되는 응답이다. M6 게이트는 (a)(b)만 구현한다.

**E. 운영·라우터**
21. 정답성 수집은 동시성 4로 하되 동시성을 기록한다. P95 지연 판정(단순 RAG 전체 ≤ 5s)은 **동시성 1** 별도 실행에서만 한다(M7).
22. 실행 전 dry-run이 필수이고 월 상한(`MONTHLY_BUDGET_USD`)을 넘기기 전에 멈춘다.
23. **라우터 착수 조건은 test가 아니라 dev로 정한다**(test로 정하고 test로 평가하면 누수). 데이터는 M6 dev 러너가 선택 모델로 만든 두 시스템의 dev 답변(골드 dev 20 + 실버 dev 55)이며 M7에서 Gemini로 채점하고 추가 실행은 없다. 대상 유형은 `expected_response=answer`이고 dev 문항이 8개 이상인 유형이다(현재 identifier 13, multi_doc 12, semantic 20, temporal 10). 착수 조건은 유형별 평균 차이(Agentic − 단순, 0~3 척도) 중 +0.5 이상인 유형이 1개 이상이고 −0.5 이하인 유형도 1개 이상일 때이며 비용·지연은 넣지 않는다. 라우팅 표: 조건을 만족한 유형은 dev 승자로 보내고, 나머지 유형(차이 0.5 미만이거나 문항 8개 미만인 sql·table 등)은 Agentic으로 보낸다. 평가는 라우터를 제3 시스템으로 골드 test에서 실행해 Agentic과 짝 비교하며 기술 통계와 95% CI만 보고하고 사전 등록 가설이 아님을 표시한다. 유형 분류기(정답 라벨 사용 vs 모델 분류)는 M7이 정한다.

### 4. `docs/EVAL.md`
- '실행 조건'의 `답변 모델 temperature 0. 모델명·버전·프롬프트 해시를 결과와 함께 기록한다.`를 `답변 모델은 reasoning.effort를 고정하고, temperature를 지원하지 않는 모델은 보내지 않고 null로 기록한다. 모델명·버전·프롬프트 해시를 결과와 함께 기록한다.`로 바꾼다.
- 파일 끝에 `## 변경 기록` 표(날짜 | 변경 | 이유)를 만들고 위 변경을 `2026-09-30 | 답변 모델 temperature 0 → reasoning.effort 고정, temperature 미지원 모델은 null 기록 | 답변 모델 gpt-6-luna·gpt-6.1-sol은 reasoning 모델이라 effort가 none이 아니면 temperature·top_p 미지원(ADR-029)`로 기록한다(EVAL.md에는 지금 변경 기록 표가 없다).
- '실행 조건'에 M6 결정론 지표 정의를 추가한다: 정답 값 포함률(규칙 11), 인용 정밀도(인용 문서 ∈ 정답 문서 목록), 거절 정확도, 게이트 누설 (a)(b), 시스템별 단계 수·토큰·비용·구간 지연.

### 5. `docs/ARCHITECTURE.md`
- 디렉토리 구조에 `tools/  # 도구 4개 계약(스키마·버전·오류·결과 축약)과 run_tool — agent·mcp_server가 retrieval을 부르는 유일한 경로`를 추가하고, `agent/` 설명에 "LangGraph 그래프·도구 래퍼·answer(), llm/ ManagedChat 사용"을 반영한다. `logs/traces/`는 유지한다.
- '패턴'에 계층 규칙을 추가한다: `retrieval`은 `tools`·`agent`·`mcp_server`·`ui`를 import하지 않는다, `tools`는 `agent`·`mcp_server`·`ui`·`kodocs.llm`·`langchain*`·`langgraph*`·`mcp`를 import하지 않는다, `agent`·`mcp_server`는 `retrieval`을 `tools`를 통해서만 쓴다.
- 'MCP 계약'에 오류 코드 의미(ADR-029 8번), 스냅샷·버전 규칙, in-process/MCP parity, 토큰 저장(평문은 `data/interim/mcp/tokens.json` 비커밋, 저장소에는 sha256 해시만 `data/manifest/mcp_token_hashes.json`)을 반영한다.

### 6. `AGENTS.md`
- '아키텍처 규칙'에 CRITICAL 두 줄을 추가한다: `agent`·`mcp_server`는 `retrieval`을 `tools`를 통해서만 호출한다(계층 가드 테스트로 강제) / `make m6-tokens`가 만든 평문 토큰은 gitignore된 파일에만 두고 출력·커밋·트레이스에 남기지 않는다.
- 명령어 목록은 각 구현 step이 자기 명령을 추가하므로 이 step에서는 건드리지 않는다.

## Acceptance Criteria

```bash
make verify   # ruff check + ruff format --check + pytest
grep -n "ADR-029" docs/ADR.md
grep -n "M6 판정 규칙" docs/results/hypotheses.md
grep -n "src/kodocs/tools\|tools/" docs/ARCHITECTURE.md
test "$(grep -c '보완(M5)\*\*: H7이 반대 방향으로 기각됨' docs/ADR.md)" = "2"
grep -n "reasoning.effort" docs/EVAL.md
! grep -n "답변 모델 temperature 0" docs/EVAL.md
! grep -n "TBD" docs/ADR.md docs/results/hypotheses.md docs/EVAL.md docs/ARCHITECTURE.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, run_sql 제한, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock)을 위반하지 않았는가?
   - 규칙 1~23이 번호 그대로 hypotheses.md에 있고 숫자(8청크·6,000자·5·100,000·30/45/120초·4,000)가 ADR-029와 일치하는가?
   - ADR-015·025 본문이 바뀌지 않고 보완 한 줄만 추가됐는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 코드·테스트·Makefile을 수정하지 마라. 이유: 이 step은 사전 등록 문서만 만든다(M5 step 0과 같은 구조).
- 규칙 숫자를 임의로 바꾸거나 '추후 결정'으로 남기지 마라. 이유: 결과를 보기 전에 고정해야 하는 값이다(ADR-026).
- ADR-015·025 본문·다른 ADR의 기존 문장을 고치지 마라. 이유: 보완 한 줄만 허용된 범위다.
- 네트워크 요청·다운로드·패키지 설치를 하지 마라(이 step이 명시한 의존성 추가 제외). 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 더 필요해 보이면 `blocked`로 보고한다.
- 실제 LLM·외부 API를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- M5 확정 산출물(`data/manifest/m5_config.json`, `data/manifest/m5_test_results/`, `data/manifest/m5_test_runs.jsonl`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5 판정 규칙을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026).
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 기존 테스트를 깨뜨리지 마라
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
