# PRD: ko-docs-agentic-rag

## 목표
한국어 사내 문서에 대해, 질의 유형에 맞는 도구를 골라 쓰는 Agentic RAG를 만들고 **검색·답변·보안을 각각 수치로 증명**한다. 무엇이 성능을 바꿨는지 한 번에 한 변수씩 측정한 결과표가 핵심 산출물이다.

## 사용자
- 가상 IT 서비스 회사(약 200명)의 임직원. 팀·직급에 따라 볼 수 있는 문서가 다르다.
- 외부 에이전트·클라이언트(프로젝트 B의 에이전트, Claude Code 등). MCP로 같은 검색 도구를 쓴다.

## 코퍼스
- **실제 문서:** IT 계열 공공기관(KISA·NIA·NIPA 중 1곳) ALIO 공시 내부규정을 가상 회사 규정집으로 가공. HWP·PDF, 조·항·호 구조, 별표 표, 개정 이력.
- **합성 문서:** 시드 데이터(인물·서비스·사건 타임라인·티켓 DB)에서 생성한 회의록·주간보고·장애 보고서·런북·가이드·공지 약 200건과 티켓 약 500행·결재 약 150행. 기간 2025-01 ~ 2026-06.
- **조직:** 플랫폼개발·결제서비스·데이터AI·SRE인프라·정보보안·인사·재무·법무컴플라이언스.
- **권한:** 전사 공개 / 팀 한정 / 제한 3단계. 제한 문서에는 누설 판정용 카나리아 사실을 심는다.
- **식별자:** `PAY-1234`(티켓), `EXP-2026-0412`(결재), `INC-2026-031`(장애), `여비규정 제8조`(조항), `billing-api`(서비스).

## 핵심 기능
1. **질의 유형별 검색 경로:** 식별자 정확 일치, SQL 집계(티켓·결재), 하이브리드 검색(BM25 + k-NN, RRF·정규화). 모두 권한 pre-filter 적용.
2. **Agentic RAG (LangGraph):** 도구 4개(`search_docs`, `lookup_id`, `run_sql`, `get_document`), 가드레일(MAX_STEPS=5, 토큰 상한, 타임아웃), 인용 필수, 근거 없으면 거절. 비교 기준으로 단순 RAG도 같은 부품으로 구현.
3. **MCP 서버:** 같은 도구 4개를 streamable HTTP로 노출. bearer 토큰으로 사용자를 식별하고 서버가 권한을 결정한다. 프로젝트 B의 에이전트가 이 서버의 클라이언트가 된다.
4. **평가 하네스:** 파싱·검색·답변·보안·운영 지표를 산출하고 결과표를 `docs/results/`에 남긴다. 지표 정의는 `docs/EVAL.md`.

## 마일스톤 (하네스 phase = PR 1개)
| # | phase | 산출물 | 사용자 작업 |
|---|---|---|---|
| M0 | setup | uv 프로젝트, `make verify`, OpenSearch+nori compose, `.env.example` | API 키 4개 `.env` 입력 |
| M1 | data-collection | ALIO 사규 수집 스크립트, manifest, HWP→PDF 변환 | 대상 기관 확정(형식·공공누리 확인) |
| M2 | parsing | pypdf·Docling·업스테이지 파서, 조문 분할기, 파싱 결과표 | 표 정답 약 10개 검수 |
| M3 | synthetic-corpus | 시드 데이터 YAML, 생성 파이프라인, 카나리아, 티켓·결재 SQLite | `make generate` 실행 |
| M4 | golden-set | 50문항 초안·qrels, dev/test 분리 | 50문항 전부 검수 |
| M5 | retrieval | 인덱스(분석기 변형), BGE-M3, hybrid, 권한 filter, `lookup_id`, `run_sql`, 검색 실험표 | 검색 평가 실행 |
| M6 | agent-mcp | 단순 RAG·Agentic RAG 그래프, 도구, 가드레일, `answer()` 인터페이스, MCP 서버 | — |
| M7 | answer-eval | Gemini 채점, κ 검증, 누설률, 운영 지표, 부트스트랩 CI, 최종 결과표 | 답변 50개 직접 채점 |
| M8 | wrap-up | 리랭커 실험, README 결과표, ADR 정리 | 공개 전환 결정 |

질의셋(M4)은 검색 구현(M5) 전에 확정한다. 검색을 만든 뒤 질의를 만들면 잘 되는 질의 쪽으로 치우친다.

## MVP 제외 사항
- 웹 UI (CLI·MCP로 충분)
- 쓰기 도구 (문서 수정·결재 실행 등. 읽기 전용만 제공)
- 원문 증분 동기화·실시간 인덱싱
- 모델 파인튜닝

## 성공 기준
- 검색 실험표: 분석기(standard·n-gram·nori) × 방식(BM25·k-NN·hybrid RRF·정규화) × 청킹(구조·고정)을 질의 유형별 NDCG@10·Recall@10과 부트스트랩 95% CI로 보고
- 단순 RAG 대 Agentic RAG를 질의 유형별 정답성·지연·비용으로 비교
- 누설률 0% (in-process 경로와 MCP 경로 모두)
- 채점 모델과 사람 채점의 가중 Cohen's κ ≥ 0.6 (미달 항목은 사람 채점으로 보고)
- 단순 RAG 지연 P95 ≤ 5s
