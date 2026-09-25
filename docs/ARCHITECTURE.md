# 아키텍처

## 디렉토리 구조
```
src/kodocs/
├── ingest/       # ALIO 사규 수집, HWP → PDF 변환, manifest 기록
├── parsing/      # 파서 어댑터(pypdf·Docling·업스테이지), 조문 분할(`제N조` 정규식)
├── chunking/     # 구조 기준 청킹(조·별표 표·안건·섹션), 고정 길이 청킹
├── synth/        # 시드 데이터 → 합성 문서·티켓·결재 생성 (Anthropic API)
├── retrieval/    # OpenSearch·SQLite 검색, authorized_* (langchain·langgraph import 금지)
├── agent/        # LangGraph 그래프(단순 RAG, Agentic RAG), 도구 래퍼, answer()
├── mcp_server/   # MCP 서버 (streamable HTTP, bearer 토큰 → 사용자 컨텍스트)
└── llm/          # Anthropic·OpenAI·Gemini 호출 래퍼 (모델명·온도 기록)
eval/             # 평가 러너, 지표, 부트스트랩, 결과표 생성
data/
├── manifest/     # 원문 출처·해시·라이선스 (커밋)
├── raw/          # 다운로드 원문·변환 PDF (커밋 안 함)
├── seed/         # 시드 데이터 YAML (커밋)
├── synthetic/    # 생성 문서·메타데이터 (커밋)
└── golden/       # 질의셋·qrels·사람 채점 (커밋, 사람만 수정)
docs/results/     # 실험 결과표 (날짜·커밋 해시 포함)
infra/            # docker-compose (OpenSearch + nori)
tests/
```

## 패턴
- **검색은 순수 Python, 조율만 LangGraph.** `retrieval`은 OpenSearch·SQLite를 직접 호출한다. LangGraph 도구와 MCP 도구는 `retrieval`을 부르는 얇은 래퍼다.
- **단일 인가 지점.** 모든 경로가 `retrieval.authorized_*`를 거친다. 입구(LangGraph, MCP)가 늘어도 권한 규칙은 한 곳에 있다.
- **공통 답변 인터페이스.** `answer(question, user_ctx) -> {answer, citations, refused, trace}`. 단순 RAG와 Agentic RAG가 이를 구현하고, 평가는 이 인터페이스만 호출한다.
- **파서·청커 교체 가능.** 파서 어댑터는 같은 출력 형식을 내고, 조문 분할은 파서와 독립이다. 실험 시 한 부품만 바꾼다.

## 데이터 흐름
```
[적재]
ALIO 사규(HWP/PDF) → HWP→PDF → 파서 → 조문 분할 → 구조 청킹 ─┐
시드 데이터 YAML → 합성 문서(DOCX/XLSX/MD/HTML/PDF) → Docling → 청킹 ┼→ BGE-M3 → OpenSearch (권한·식별자·날짜 메타데이터)
시드 데이터 YAML → 티켓·결재 ────────────────────────────→ SQLite

[질의]
사용자(팀·직급) → answer() ─ 단순 RAG: search_docs 1회 → 답변
                          └ Agentic RAG(LangGraph): 도구 선택 반복(≤5) → 답변(인용 필수)
외부 클라이언트(B, Claude Code) → MCP(bearer 토큰) → 같은 도구
                          모든 도구 → retrieval.authorized_* → OpenSearch / SQLite
```

## 상태 관리
- **인덱스:** OpenSearch 인덱스는 실험 변형(분석기·청킹)별로 이름을 나눠 만든다. 재생성 가능한 산출물이다.
- **정형 데이터:** SQLite 파일은 시드 데이터에서 재생성한다. `run_sql`은 읽기 전용으로 연다.
- **권한:** 문서 메타데이터 `access = {level: public|team|restricted, teams: [...], min_grade}`. 사용자 컨텍스트는 서버가 결정한다(LangGraph `ToolRuntime` context, MCP bearer 토큰 매핑).
- **비밀 값:** API 키는 `.env`에서만 읽는다.
