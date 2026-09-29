# Step 0: design-docs

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — `data/golden/`은 사람이 확정, 원문 비커밋, 외부 LLM은 `src/kodocs/llm/` 래퍼로만
- `docs/PRD.md` — M4 행, "질의셋은 M5 전에 확정"
- `docs/ARCHITECTURE.md` — `data/golden/`, `docs/results/` 역할
- `docs/ADR.md` — ADR-001, ADR-002, ADR-012, ADR-013, ADR-015, ADR-016, ADR-019, ADR-024
- `docs/EVAL.md` — 질의셋·지표·보안·결과 보고 절 전체
- `docs/results/2026-09-27-m3-synthetic.md` — 결과 문서 형식(고정 파일 하나, 생성 영역 `<!-- BEGIN generated -->`/`<!-- END generated -->`)
- `src/kodocs/eval/templates/m3_synthetic.md` — 결과 문서 템플릿 형식

## 배경

M4는 검색 구현(M5) 전에 질의셋·정답(qrels)·dev/test 분리·가설을 확정한다. 이 step은 코드 없이 결정 문서만 쓴다. 이후 step은 이 문서를 기준으로 구현한다. 아래 수치와 규칙은 사용자와 확정한 값이므로 바꾸지 마라.

## 작업

### 1. `docs/ADR.md`에 ADR-025 추가 — 골드셋 설계

기존 ADR 형식(**결정**/**이유**/**트레이드오프**/필요 시 **변경 기록**)을 따른다. 결정에 아래를 모두 적는다.

- **출처·유형(EVAL 8유형 유지)**. 골드 50 = 실제 규정 20 + 합성 30. 유형 × 출처:

  | 유형(type) | 실제 | 합성 | 계 |
  |---|---:|---:|---:|
  | identifier(식별자) | 2 | 5 | 7 |
  | semantic(단일 문서 의미) | 4 | 5 | 9 |
  | table(표/별표) | 6 | 0 | 6 |
  | multi_doc(다중 문서) | 2 | 6 | 8 |
  | temporal(최신성·시점) | 5 | 0 | 5 |
  | permission(권한) | 0 | 5 | 5 |
  | unanswerable(답 없음) | 1 | 3 | 4 |
  | sql(SQL 집계) | 0 | 6 | 6 |

  실제 규정에는 권한 메타데이터와 정형 데이터가 없으므로 permission·sql은 전부 합성이다.
- **실버 150(전부 합성)**: identifier 25, semantic 40, multi_doc 35, temporal 20, permission 15, unanswerable 15. multi_doc를 35로 둔 이유: H5 묶음(식별자+SQL+다중)의 방향 확인 표본을 늘리고, 합성 코퍼스에서 문서 간 연결이 가장 풍부한 유형이어서.
- **시점 태그 분리**: `temporal:revision`(실제 개정 이력, 골드 temporal 5 전부)과 `temporal:as_of_event`(합성, 실버 temporal 20 = 기준일로 특정되는 사건 19 + 값 교체 1). 결과는 태그별로 따로 보고한다. `temporal:as_of_event`는 골드 대응 문항이 없어 실버로 골드 방향을 확인할 수 없다.
- **시점 정의 변경 근거(실측)**: 시드 사실 428개를 이벤트 단위로 묶어 셌을 때 같은 대상의 속성 값이 교체된 경우는 identity-api 담당자(2026-03-28 p-002 → 2026-05-09 p-133) 1건뿐이다. 반면 서비스 12개에 날짜가 다른 사건 70개(장애 25·이벤트 45), 답이 될 사실 195개가 있어 기준일 없이는 답이 갈린다. 같은 서비스·같은 달 충돌이 18건이라 기준일은 일 단위로 준다.
- **qrels(ADR-016)**:
  - 합성: 그 `fact_id`의 span을 가진 **모든** 통과 문서 = 관련도 2, 같은 `subject`(사건·이벤트)를 다루지만 그 사실 span이 없는 문서 = 관련도 1. 원자 사실 값이 표시 없이 나온 문서는 "관련 후보(미표시)"로 검수 패킷에 올려 사람이 정한다. 표시: `doc_id + fact_id + [start, end)` + span 텍스트 SHA-256.
  - 실제: `doc_id = "law-{serial}"`(법제처 버전 serial) + 조 키(`제N조`, `제N조의M`, `data/manifest/regulation_refs.json`에 있어야 함). 표 문항은 `data/manifest/table_golden.json`의 표 `id`. 최신성 문항은 `data/manifest/version_matches.jsonl`의 `confidence`가 `exact`·`approximate`인 버전만.
  - SQL: 기대 값은 시드에서 결정론으로 만든 SQLite로 계산하고, 쿼리 SHA-256과 `data/manifest/synth_db.json`의 `content_hash`를 함께 기록한다.
- **원문 제약(ADR-002)**: 실제 문항은 질문·조 키·요지(80자 이하)만 커밋한다. 조문을 인용한 참조 답변과 검수 패킷은 `data/interim/golden/`(비커밋)에 두고 SHA-256을 manifest에 기록한다.
- **질문자·권한**: 문항마다 질문자(팀·직급). qrels는 권한과 무관하게 전체를 표시하고, 질문자가 볼 수 있는 관련 문서는 access 메타데이터로 계산한다. permission 문항의 정답 라벨은 "거절"이 아니라 `expected_response = "indistinguishable_from_no_answer"`: 권한 밖 사용자에게 준 응답이 답 없음 문항의 응답과 같은 종류여야 한다(권한 부족 언급, 제한 문서의 제목·ID·존재 암시 금지). 나머지 답할 수 있는 문항은 `"answer"`, unanswerable은 `"no_answer"`.
- **답 없음 3단계 확인**: ① 시드 사실 ② 통과 문서 본문 문자열 검색 ③ 실제 규정 조문 본문 문자열 검색. 셋 다 비어야 채택하고 결과를 입력 해시와 함께 manifest에 남긴다.
- **dev/test 분리**: 관련 문서가 겹치는 문항을 한 그룹으로 묶어(union-find) 그룹 단위로 배정하고 유형별로 층화한다. 실제 문항은 규정(`rule_id`) 단위로 묶는다. 골드 test 30/dev 20, 유형별 할당은 아래 표를 그대로 쓴다.

  | | identifier | semantic | table | multi_doc | temporal | permission | unanswerable | sql | 계 |
  |---|---|---|---|---|---|---|---|---|---|
  | test | 4 | 5 | 4 | 5 | 3 | 3 | 2 | 4 | 30 |
  | dev | 3 | 4 | 2 | 3 | 2 | 2 | 2 | 2 | 20 |

  실버도 같은 규칙으로 test 60%·dev 40%(유형별 반올림, 합 150)로 나눈다. 남은 dev↔test 문서 겹침 수와 할당 이탈을 결과 문서에 보고한다.
- **어휘 누수·답 값 누수**: 질의 ↔ 관련도 2 구간 텍스트의 글자 바이그램 포함률(질의 바이그램 중 구간에도 있는 비율)과 최장 공통 부분 문자열 길이. 초기 상한은 포함률 ≤ 0.6, 최장 공통 부분 문자열 ≤ 12자이며 초안 실측 분포를 본 뒤 확정한다(확정값과 근거를 이 ADR 변경 기록에 추가). 포함률 ≤ 0.2이면 `lexical_mismatch` 태그. 질문에는 주어(장애 번호·서비스·인물·기준일)만 허용하고 답 값은 금지한다(identifier 유형의 조회 키 자체는 주어로 허용).
- **질의 문장 생성**: 합성 질의 문장만 Anthropic Batch(Sonnet 5)로 바꿔 쓴다. 정답·유형·기준일은 결정론 명세가 정하고, 모델은 정답 구간 문장과 답 값을 보지 않는다. 실제 규정 문항은 API로 보내지 않는다.
- **검수**: "Claude 판단 초안 + 사람 확인". 검수 패킷은 Claude 판정을 미리 채우고, 사람은 동의하지 않는 항목만 적는다(기본값 수락). 도구는 `reviewer`·`reviewed_on`을 절대 미리 채우지 않는다. 선택 기능으로 점검용 더미 문항 3개(Claude 판정을 일부러 틀리게 함)를 패킷에 섞고 적발 수를 보고한다. 점검용 문항은 골드 파일·qrels·split에 들어가지 않는다.
- **변경 기록**: EVAL.md의 "사규 문항은 사람이 사규를 읽고 작성한다"를 "Claude가 로컬 조문을 읽어 초안을 쓰고 사람이 전부 확인·확정한다"로 바꾼다. 이유: 20문항 작성 시간과 형식 일관성(조 키·요지 형식을 도구가 검증). 사람 확정은 유지하므로 골드의 근거 수준은 같다. 트레이드오프: 초안이 판단을 끌어가는 확인 편향 위험 → 결과 문서 한계에 적고 점검용 더미로 일부 측정한다.

### 2. `docs/ADR.md`에 ADR-026 추가 — 가설 사전 등록

- 결정: 검색 구현(M5) 전에 `docs/results/hypotheses.md`를 확정한다. 결과를 본 뒤 가설·판정 대상 문항 수·판정 방법을 바꾸지 않는다. 문구를 바꿔야 하면 날짜·사유·변경 전 문구를 파일에 남긴다. 결과 보고 때 가설마다 채택/기각/판정 불가와 원인을 적는다.
- 이유: 결과를 보고 가설을 고르면 우연한 차이를 주장하게 된다(EVAL "가설 사전 등록").

### 3. `docs/results/hypotheses.md` 새로 작성

공통 기준(파일 첫머리): 짝 비교 부트스트랩 10,000회, 95% CI가 0을 포함하지 않으면 채택, 포함하면 "판정 보류(차이 입증 못 함)". 유형별 수치는 참고용(EVAL "유형별 수치는 문항 수가 적어 참고용"). 보고는 골드 test만.

"검색 지표 대상" = qrels가 비어 있지 않은 유형(identifier·semantic·table·multi_doc·temporal)의 test 문항 = 21문항.

| ID | 가설 | 판정 대상(문항 수 고정) | 방법 | 검정력 한계 | 검증 phase |
|---|---|---|---|---|---|
| H1 | nori > standard (문서 단위 Recall@10) | 골드 test 검색 지표 대상 21 | 짝 비교 부트스트랩 | n=21이라 약 0.05 수준 차이는 CI가 0을 포함하기 쉽다 | M5 |
| H2 | hybrid > BM25(nori) (문서 단위 NDCG@10) | 같은 21 | 짝 비교 부트스트랩 | `lexical_mismatch` 태그 부분집합은 기술 통계만(판정하지 않음) | M5 |
| H3 | 구조 청킹 > 고정 청킹 (구간 겹침 적중률) | 같은 21 | 짝 비교 부트스트랩 | H1과 같음 | M5 |
| H4 | 식별자 keyword 정확 일치의 1위 적중률 > 본문 검색 | 골드 test identifier 4 | 짝 비교 부트스트랩 | 4문항이라 사실상 판정 불가. 실버 identifier test 문항으로 방향만 확인 | M5 |
| H5 | Agentic RAG > 단순 RAG (정답성 0~3) | 골드 test identifier+sql+multi_doc 묶음 13 | 짝 비교 부트스트랩(묶음 합산) | 유형별은 기술 통계만. 나머지 유형에서 차이가 없다는 것은 증거 부족일 뿐 동등성 주장이 아니다 | M6·M7 |
| H6 | 누설률 0% (in-process·MCP, 간접 인젝션·존재 누설 포함) | M7 보안 테스트셋 200건 이상 | 관측 누설 건수 | 0/200이면 누설률 95% 상한 약 1.5%(3의 법칙). "0%"는 관측값이지 증명이 아니다 | M7 |
| H7 | 합성 문항 점수 > 실제 문항 점수 (문서 단위 Recall@10) | 골드 test 중 양쪽 출처에 모두 있는 유형(identifier·semantic·multi_doc)만. 출처별 문항 수는 split 확정 후 고정 | **독립 표본 부트스트랩**(문항이 달라 짝 비교 불가) | 전체 비교(모든 유형)는 유형 구성이 달라(실제=표·최신성 위주, 합성=식별자·다중 위주) 기술 통계로만. 판정 표본이 작아 방향만 기대한다 | M5 |

H7의 판정 문항 수는 split 확정 후 이 파일에 숫자로 고정한다는 문장을 넣고, 그 자리는 `(split 확정 후 기록: 합성 n=?, 실제 n=?)`로 둔다(사람이 split 결과를 보고 채운다). 파일 끝에 "변경 기록" 절을 빈 표로 둔다.

### 4. `docs/EVAL.md` 수정

- "출처" 줄 옆 "사규 문항은 사람이 사규를 읽고 작성한다" → "사규 문항은 Claude가 로컬 조문을 읽어 초안을 쓰고 사람이 전부 확인·확정한다(ADR-025)".
- 골드셋 절에 위 유형 × 출처 표와 test/dev 할당 표를 추가하고, "최신성" 유형 설명에 태그 `temporal:revision`을 붙인다.
- 실버셋 절에 유형 배분(합 150)과 `temporal:as_of_event` 정의(기준일 명시 사건 질의, 기준일 없이는 후보 사건 2개 이상)를 추가하고, "결과는 시점 태그별로 따로 보고, as_of_event는 골드 대응이 없어 방향 확인 불가"를 적는다.
- 문항 필드 줄에 `expected_response`(answer | no_answer | indistinguishable_from_no_answer)와 `anchor_values`·`answer_values`를 추가한다.
- 보안 절 판정에 "(c) 권한 밖 정답 문항에서 답 없음 문항과 구별되는 응답(권한 부족 언급, 제한 문서 제목·ID·존재 암시)"을 추가한다.
- 가설 사전 등록 절에 ADR-026과 `docs/results/hypotheses.md` 참조를 적는다.

### 5. M4 결과 문서 템플릿

- `src/kodocs/eval/templates/m4_golden.md` 생성. M3 템플릿처럼 첫 부분에 `<!-- BEGIN generated -->`/`<!-- END generated -->` 한 쌍을 두고, 생성 영역에는 자리표시 `(실행 후 기록)`만 둔다. 생성 영역 밖 고정 절: "사람 검수 절차(기본값 수락·행별 서명·점검용 더미)", "한계"(합성 문항은 실제보다 쉽다, `temporal:as_of_event`는 골드 대응이 없어 실버 방향 확인 불가, 기본값 수락의 확인 편향, 실제 문항 초안을 Claude가 씀), "변경 기록".
- 최종 결과 문서 경로는 `docs/results/2026-09-28-m4-golden-set.md` 하나로 고정한다(실행일로 파일명을 만들지 않는다. 이 파일은 step 7의 도구가 만든다. 이 step에서는 만들지 마라).

### 6. `AGENTS.md`

- 명령어 절은 이 step에서 바꾸지 않는다(각 step이 자기 명령을 추가한다).

## Acceptance Criteria

```bash
make verify
grep -n "ADR-025\|ADR-026" docs/ADR.md
grep -n "temporal:revision\|temporal:as_of_event\|indistinguishable_from_no_answer" docs/EVAL.md docs/ADR.md
test -f docs/results/hypotheses.md && grep -n "H7" docs/results/hypotheses.md
test -f src/kodocs/eval/templates/m4_golden.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 유형 × 출처 표의 합이 행·열 모두 맞는가(실제 20, 합성 30, 계 50)? 실버 합이 150인가? test 30·dev 20인가?(FP-043: 수량 기준은 무엇을 세는지까지 적는다)
   - AGENTS.md CRITICAL 규칙(원문 비커밋, golden은 사람이 확정)과 충돌하는 문장이 없는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 위 표의 수치(유형 × 출처, 실버 배분, test/dev 할당, 초기 상한)를 바꾸지 마라. 이유: 사용자와 확정한 값이고 가설의 판정 문항 수가 여기에 묶여 있다.
- 코드·테스트·Makefile을 만들거나 바꾸지 마라. 이유: 이 step은 결정 문서만 다룬다(scope 최소화).
- `data/golden/` 아래 파일을 만들거나 바꾸지 마라. 이유: 사람만 수정한다(AGENTS.md).
- 조문·별표 본문을 문서에 옮기지 마라. 이유: ADR-002 원문 비커밋.
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
