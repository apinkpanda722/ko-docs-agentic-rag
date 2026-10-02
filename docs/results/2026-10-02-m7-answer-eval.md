# M7 답변 평가 결과

<!-- BEGIN generated -->

## 핵심 결과 요약

### PRD 성공 기준 6개 체크표

| 기준 | 상태 | 핵심 수치 |
| --- | --- | --- |
| 검색 실험표 | 달성 | [M5 검색 사다리·제거 실험·전체 조합](2026-09-30-m5-retrieval.md) |
| 사전 등록 가설·원인 분석 | 부분 달성(7개 모두 사전 등록 규칙대로 판정·원인 분석 완료, H2·H3·H4는 판정 보류) | 아래 H1~H7 판정 표 |
| 단순 대 Agentic 유형별 비교·실패 원인 | 달성 | H5 +1.615 [0.923, 2.308], n=13 |
| 보안 누설률 0% | 달성 | 4경로·시스템 누설 0/200, Clopper-Pearson 상한 0.018 |
| κ ≥ 0.6 | 달성 | 0.903 [0.767, 0.976] |
| 단순 RAG P95 ≤ 5s | 달성 | P95 3.732s [3.090, 6.598], 목표 5.0s |

### H1~H7 판정

| 가설 | 판정 | 차이 [95% CI] | n | 비교·보조 |
| --- | --- | --- | --- | --- |
| H1 | 채택 | +0.105 [0.036, 0.184] | 21 | nori BM25 고정 - standard BM25 고정 (recall10) |
| H2 | 판정 보류(차이 입증 못 함) | +0.020 [0.000, 0.047] | 21 | nori hybrid 고정 - nori BM25 고정 (ndcg10) |
| H3 | 판정 보류(차이 입증 못 함) | +0.048 [0.000, 0.143] | 21 | nori hybrid 구조 - nori hybrid 고정 (span_hit) |
| H4 | 판정 보류(차이 입증 못 함) | +0.250 [0.000, 0.750] | 4 | keyword - 최종 구성 본문 검색 (top1_hit) |
| H5 | H5 채택: Agentic RAG의 정답성이 단순 RAG보다 높다(차이 +1.615, 95% CI [0.923, 2.308], n=13). | +1.615 [0.923, 2.308] | 13 | 유형별 차이 identifier 0.000, multi_doc +1.800, sql +3.000; sql 제외 +1.000 [0.333, 2.000] |
| H6 | 관측 누설 0건, 4경로·시스템 모두 H6 채택(관측값) | 누설 0/200 | 경로·시스템당 200 | 경로·시스템별 판정은 H6 표, 근거는 부록의 보안 절 |
| H7 | 기각(반대 방향) | -0.448 [-0.729, -0.146] | 합성 8·실제 6 | 합성 - 실제 (recall10) |

H1~H4·H7의 값과 원인 분석은 [M5 검색 사다리·제거 실험·전체 조합](2026-09-30-m5-retrieval.md)에서 옮겼다.

### H5 유형별·보조 비교(정답성, 0~3점)

| 비교 | n | Agentic | 단순 | 차이 [95% CI] |
| --- | --- | --- | --- | --- |
| H5 identifier | 4 | 2.250 | 2.250 | 0.000 |
| H5 multi_doc | 5 | 2.200 | 0.400 | +1.800 |
| H5 sql | 4 | 3.000 | 0.000 | +3.000 |
| H5 전체 | 13 | 해당 없음 | 해당 없음 | +1.615 [0.923, 2.308] |
| sql 제외 | 9 | 해당 없음 | 해당 없음 | +1.000 [0.333, 2.000] |
| 실버 방향 확인(판정 아님) | 29 | 해당 없음 | 해당 없음 | +0.747 [0.276, 1.253] |
| 사람 기준 | 13 | 해당 없음 | 해당 없음 | +1.692 [0.923, 2.462] |

### H6 경로·시스템별(비밀 정보 누설)

| 경로·시스템 | 누설 | Clopper-Pearson 95% 상한 | c1 후보/확정 | 대조군 오거절 |
| --- | --- | --- | --- | --- |
| in-process\|agentic | 0/200 | 0.018 | 3/0 | 0/26 |
| in-process\|simple | 0/200 | 0.018 | 3/0 | 0/26 |
| in-process\|tool | 0/200 | 0.018 | 0/0 | 0/26 |
| mcp\|tool | 0/200 | 0.018 | 0/0 | 0/26 |

사후 정정 전 누설 판정 3건(c2 식별자 고유성 결함 — 공개본·팀본 같은 제목)은 정정 후 0건이다. 정정 전·후를 모두 부록에 보존한다. 도구 경로 대조군 오거절은 정정 전 in-process|tool 1/26, mcp|tool 1/26에서 정정 후 도구 호출 오류 in-process|tool 1/26, mcp|tool 1/26로 재분류돼 오거절 0건이다.

### 지연(스트리밍 모드, 동시성 1, 예열 제외, 단위 초)

| 시스템 | P50 | P95 [95% CI] | TTFT P50 / P95 | 검색 P50 / P95 | 실행 / 실패 |
| --- | --- | --- | --- | --- | --- |
| 단순 RAG | 1.887 | 3.732 [3.090, 6.598] | 0.727 / 1.381 | 0.026 / 0.035 | 113 / 2 |
| Agentic RAG | 6.716 | 13.461 [10.875, 15.504] | 0.735 / 1.212 | 0.027 / 0.068 | 113 / 5 |

비캐시 보정 상한 = 3.732 + 0.378 = 4.109s(반올림 전 값의 합, 판정 아님, 보수적 상한).

- Sol 사후 신호(판정 아님): Sol − Luna +0.167 [0.017, 0.333], n=59, dev 비용 약 18배(Luna $0.095, Sol $1.721) — 재검토 신호.
- 라우터 미착수(dev 유형별 차이 조건 미충족).

세부 표와 원본 집계는 [부록(원본 집계)](2026-10-02-m7-answer-eval-appendix.md)에 있다.

## 실행 정보

| 항목 | 값 |
| --- | --- |
| 고정 실행일(KST) | 2026-10-02 |
| 생성일(KST) | 2026-10-02T16:50:38.932481+09:00 |
| 생성 코드 커밋 | dcfecf8 |
| 측정 코드 커밋 | 6f9cd12f78b73de7df06c7c5226fcb50546a740b |
| m5 config_hash | 455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d |
| m6 config_hash | ba5ee42864360f3af13a26b4581b0ce6044c395c0aef524af6195c5a98d199ef |
| m7 config_hash | ddda565281aa3c36797b325a367a97989717413ae8ede48a525e3f8f31f582d2 |

## 단순 대 Agentic 정답성 유형별·실제/합성 분리(기술 통계)

| 세트 | 출처 | 유형 | 시스템 | n | 정답성 평균(기술 통계) |
| --- | --- | --- | --- | --- | --- |
| test_gold | real | identifier | agentic | 1 | 3 |
| test_gold | real | identifier | simple | 1 | 3 |
| test_gold | real | multi_doc | agentic | 2 | 2.5 |
| test_gold | real | multi_doc | simple | 2 | 1 |
| test_gold | real | semantic | agentic | 3 | 3 |
| test_gold | real | semantic | simple | 3 | 2 |
| test_gold | real | table | agentic | 4 | 1.25 |
| test_gold | real | table | simple | 4 | 2 |
| test_gold | real | temporal | agentic | 3 | 2.3333333333333335 |
| test_gold | real | temporal | simple | 3 | 2 |
| test_gold | synthetic | identifier | agentic | 3 | 2 |
| test_gold | synthetic | identifier | simple | 3 | 2 |
| test_gold | synthetic | multi_doc | agentic | 3 | 2 |
| test_gold | synthetic | multi_doc | simple | 3 | 0 |
| test_gold | synthetic | semantic | agentic | 2 | 1.5 |
| test_gold | synthetic | semantic | simple | 2 | 1.5 |
| test_gold | synthetic | sql | agentic | 4 | 3 |
| test_gold | synthetic | sql | simple | 4 | 0 |
| test_silver | synthetic | identifier | agentic | 15 | 1.9777777777777779 |
| test_silver | synthetic | identifier | simple | 15 | 1.6 |
| test_silver | synthetic | multi_doc | agentic | 14 | 1.5 |
| test_silver | synthetic | multi_doc | simple | 14 | 0.35714285714285715 |
| test_silver | synthetic | semantic | agentic | 24 | 2.388888888888889 |
| test_silver | synthetic | semantic | simple | 24 | 2.388888888888889 |
| test_silver | synthetic | temporal | agentic | 12 | 1.5555555555555556 |
| test_silver | synthetic | temporal | simple | 12 | 1.2777777777777777 |

## 비용 — 원장 정가 + 부가세, Gemini 평가 비용 분리

| 항목 | 값 |
| --- | --- |
| 운영 비용.m6-test-01 | 0.20233318050000001 |
| 운영 비용.m7-security | 0.35377829850000003 |
| 평가 비용.gemini-grade | 1.7957115000000001 |
| 별도 측정 비용.m7-latency | 0.21494963050000002 |

비용은 원장의 amount_usd(정가 + 부가세)다. 질의별 실행 기록의 비용을 재집계하고 M6 test·보안 실행의 원장 합계와 대조한다. Gemini probe·파일럿·본 채점이 같은 run_id를 쓰면 원장에는 합쳐져 있으므로 위 표는 그 run_id 그대로 보고한다. 파일럿·채점 manifest 비용은 해당 절에 별도로 병기하며 운영비에 합산하지 않는다. M6 운영 지연은 동시성 4 기술 통계이며 P95 판정은 별도 지연 절의 동시성 1 결과만 쓴다.

## 지연 비캐시 보정치(판정 아님, 보수적 상한)

지연 측정은 임베딩 캐시 적중 상태에서 이뤄졌다(캐시 적중 상태 측정). 보정 상한 = 단순 RAG P95 3.732 + Agentic 비캐시 임베딩 최대 0.378 = 4.109s(반올림 전 값의 합)다. 목표 5.0s 이내이며, 판정이 아니라 보수적 상한이다. 재측정하지 않았다.

M6 환경·dev 모델별 비용/지연 비교는 [M6 dev 비교·환경 표](2026-10-01-m6-agent-mcp.md) 참조. BGE-M3 revision 5617a9f. 패키지명 정정: 임베딩은 sentence-transformers, OpenSearch는 httpx 직접 호출이다(FlagEmbedding·opensearch-py 미설치 표기는 라이브러리 선택과 다른 수집 목록 오류). 설치 실패가 아니다. 버전은 M5/M6 환경 기록 참조.

검색은 M5 고정 구성, 답변 모델 gpt-6-luna·M6 고정 설정, 일반 답변은 M6 산출물 재사용. [사전 등록 M7 판정 규칙](hypotheses.md#m7-판정-규칙-2026-10-01-고정-m7-결과를-보기-전)·[위협 모델](../THREAT_MODEL.md) 참조.

<!-- END generated -->

## 측정 환경 보충(사용자 보존 영역)

| 항목 | 기록 |
| --- | --- |
| 측정 환경 보충·패키지명 정정 | 미기록 |
| Gemini 콘솔 Usage 실행 전 | 0원(유료 프로젝트 신규, probe 이전) |
| Gemini 콘솔 Usage 실행 후 | 2,218원(2026-10-02 확인, 사용자 보고) |
| OpenAI 콘솔 10월 누적 | $5.32(2026-10-02 확인, 사용자 보고) |
| 원장 대조·차이(FP-037) | Gemini 원장 부가세 제외 $1.633 ≈ 2,213원(1달러 1,355.84원, 2026-10-01 환율) 대 콘솔 2,218원, 차이 5원(0.2%, 환율·반올림). OpenAI 10월 원장 부가세 제외 $5.31 대 콘솔 $5.32, 차이 $0.01. 둘 다 일치로 판단(사용자 보고) |
| make test-integration 실행일·결과 | 2026-10-02, 14 passed·1942 deselected (OpenSearch 3.8.0 기동 상태, 읽기 전용) |
| 사용자 메모 | 미기록 |

## 사용자 실행 절차

1. `uv add google-genai`(사용자 직접).
2. step 0~6 실행·리뷰.
3. `make m7-probe`.
4. `make m7-pilot`.
5. 루브릭 확정·커밋.
6. `make m7-cards` → 사람 채점(정답성만) → CSV 저장 → `make m7-human-register` → 커밋.
7. `make m7-freeze`.
8. `make m7-grade-dry` → `make m7-grade`(2026-12-31 전 완료).
9. step 7~9 실행 후 `make up`과 함께 **`make m6-serve`를 별도 터미널에서 실행**한 뒤 `make m7-security-set` → `make m7-security-dry` → `make m7-security`(MCP 재생은 실행 중인 서버에 연결). c1 후보 확인 CSV 저장 후 `make m7-leak-review-register`.
10. `make m7-analyze`.
11. step 10 실행 후 `make m7-latency-dry` → `make m7-latency`(동시성 1 단독, 다른 부하 없이).
12. `make m7-failures`, `make m7-report`.
13. Gemini 콘솔 Usage와 원장 대조(FP-037), `make test-integration` 결과 기록.
14. 결과 산출물·문서 커밋 전 `make verify`.

## 한계(사용자 보존 영역)

- M6 실행 당시의 `runs.jsonl` 해시가 없다. 집계에 영향을 주지 않는 답변 텍스트 변경은 `analyze_dev()` 재집계 비교로 탐지하지 못한다(M7-D1 한계).
- 코퍼스 조립에서 일부 별표의 단위·제목·표 밖 조항이 유실되어(M5 composite의 표 대체, 사람 채점 중 law-2200000149621 별표2에서 발견) 정답 근거가 부분적으로 빠진 문항이 있다.
- 합성 질문 일부는 정답이 여럿일 수 있는데 참조 답변은 하나라서, 근거에 맞는 다른 답도 정답성 0점이 된다. 두 시스템 공통이다(파일럿 예: s-syn-044, g-syn-010; 2026-10-01 파일럿 30호출 점검에서 확인).
- 블라인드 한계: SQL 결과 인용이나 답변 형식으로 채점자(Gemini·사람)가 시스템을 추정할 수 있다.
- 충실성 지표는 사람 검증이 없다(정답성만 사람 검증).
- 답변이 시스템·문항당 1회라 비결정성이 점수에 섞인다.
- 사람 채점자가 1인이라 채점자 간 일치를 잴 수 없다.
- "스트리밍 모드에서 측정"한 지연이며 외부 API 지연 변동이 있다.
- 지연 한계 1(임베딩 캐시): 지연 측정은 임베딩 캐시 적중 상태에서 이뤄져 임베딩 구간이 약 0.001초로 나왔다. 따라서 이 지연은 캐시 적중 상태 측정이며, 본문의 "지연 비캐시 보정치" 절에 단순 RAG P95와 Agentic 비캐시 임베딩 최대값을 더한 보수적 상한(약 4.11초, 판정 아님)을 계산식과 함께 적었다. 5.0초 목표 이내이나 재측정하지 않았다.
- 지연 한계 2(스트리밍 parse_error): 스트리밍 실행에서 parse_error 4건이 있었다. 원인은 이번 마일스톤에서 확정하지 않고 M8 후보로 둔다. 후속 조사는 오프라인 로그 확인으로 제한한다(API 재호출 없음).
- 지연 한계 3(환경 기록): 환경 표의 패키지명이 실제 배포명과 달라(FlagEmbedding·opensearch-py가 미설치로 기록) 실제 사용 라이브러리는 sentence-transformers(임베딩)와 httpx(OpenSearch 직접 HTTP 호출)다. 부록의 자동 수집 원문은 그대로 두고 정정은 본문에 따로 적었으며, 임베딩 모델 BGE-M3의 revision은 5617a9f다. M8 정정 상세: [2026-10-03-m8-wrap-up.md](2026-10-03-m8-wrap-up.md).
- Sol 재검토 신호: Sol 사후 비교에서 정답성 +0.17점, 비용은 약 18배(dev 합계 Luna 약 $0.095, Sol 약 $1.72)였다. 확증이 아닌 신호이며 모델 선택 고정은 바뀌지 않는다.
- 보안 sec-0072는 도구 호출 오류(tool_error)로 분류된 사례이며 누설 판정에서 거부와 구분했다. S6 노출은 Agentic 4/32가 확정이고 나머지 26은 판정 불가로 남았다. 사람 채점 메모(`data/interim/m7/human_memos.md`, 비커밋)는 참고 자료로만 썼고 이 문서에 원문을 싣지 않았다.
- 판정기 구현 결함을 결과 확인 뒤 정정했다(c2 식별자 고유성). 정정 전·후를 모두 보고하며 임계값·분모는 바꾸지 않았다.
- 합성 코퍼스에는 같은 제목의 문서가 여럿 있다(공개본·팀본 포함).
- 단순 RAG의 S6 노출은 사전 검증과의 순위 정렬로 확정한 값이며 Agentic은 질의를 다시 써서 같은 방법을 쓸 수 없어 판정 불가 26/32가 남는다.
- 유료 등급 Gemini는 입력을 제품 개선에 쓰지 않는다([출처](https://ai.google.dev/gemini-api/docs/pricing), 확인일 2026-10-01).
- M5 H7에서 합성 문항이 실제 문항보다 쉽다는 가설은 반대 방향으로 기각됐다(합성 0.469, 실제 0.917). 이 문서도 실제·합성 결과를 분리해서 해석했다.
- H6 0건은 관측값이며 95% 상한 3/n이다. MCP 경로는 도구 수준이고 c1 판정은 사용자 확인에 의존한다.
- M6 한계인 문장형 정답 값으로 정답 값 포함률이 무력해진 점은 모델 선택 해상도를 제한한다. Sol 사후 확인은 재검토 신호다. Luna 부분 답변 경향은 보안의 부분 답변 비율·c1 기준선·거절 위반과 연결해 본다.

## M8 개선 후보(사용자 보존 영역)

별표 조립 시 표 밖 내용 유실: law-2200000149621 별표2(외부강의 사례금 상한액 표)의 코퍼스 텍스트에 단위 표기가 없다(문서 전체 '단위' 0회, 참조 답변은 "400(단위 천원)"). 2026-10-01 읽기 전용 확인: 원본 HWPX(`section0.xml`)·변환 PDF(18쪽)·pypdf·Docling·업스테이지(PDF·HWPX 직접) 파싱 결과 모두에 "(단위 : 천원)"이 있다. 유실은 파싱이 아니라 M5 composite 코퍼스 조립(`src/kodocs/chunking/corpus.py`의 `_composite_text_and_sections`)에서 일어난다: 업스테이지 표와 매칭된 별표는 pypdf 별표 영역 전체(별표 제목·단위·표 밖 조문 '2. 적용기준')를 표 마크다운만으로 대체한다(이 별표2는 pypdf 797자 → composite 293자). 같은 비교(composite 별표 vs pypdf 별표, 493개 섹션)에서 composite가 pypdf의 70% 미만인 별표 95개, pypdf에는 있고 composite에는 없는 '단위' 표기 14개 별표(10개 규정: law-2200000148327·148341·148345·148727·148765·148769·149621·150485·150587·151395)가 있다. M8 개선 후보: 매칭된 별표는 표만 대체하지 말고 pypdf 별표 영역(제목·단위·표 밖 조항)을 유지한 채 표를 병합하거나, 표 앞뒤 머리말·단위를 청크에 보존한다. M7 수치는 이 유실이 있는 채로 측정되었다. 결과에 영향이 있는 문항은 `data_preparation_notes`로 표시한다.
