# 아키텍처

## 디렉토리 구조
```
src/kodocs/
├── ingest/       # 법제처 Open API·ALIO 수집, ALIO↔법제처 버전 매칭, HWP/HWPX → PDF 변환, 파싱 대상 목록, manifest 기록
├── parsing/      # 파서 어댑터(pypdf·Docling·업스테이지), 조문 분할(`제N조` 정규식)
├── chunking/     # 구조 기준 청킹(조·별표 표·안건·섹션), 고정 길이 청킹
├── synth/        # 시드 데이터 → 합성 문서·티켓·결재 생성 (Anthropic API)
├── retrieval/    # OpenSearch·SQLite 검색, authorized_* (langchain·langgraph import 금지)
├── agent/        # LangGraph 그래프(단순 RAG, Agentic RAG), 도구 래퍼, answer()
├── mcp_server/   # MCP 서버 (streamable HTTP, bearer 토큰 → 사용자 컨텍스트)
├── llm/          # Anthropic·OpenAI·Gemini 호출 래퍼 (모델명·온도·비용 기록, 월 상한)
└── ui/           # Streamlit 데모 (answer()만 호출)
eval/             # 평가 러너, 지표, 부트스트랩, 결과표 생성
data/
├── manifest/     # 원문 출처·해시·수집일·버전 매칭·변환 기록·파싱 대상 목록 (커밋)
├── raw/          # 다운로드 원문·변환 PDF (커밋 안 함)
├── interim/      # 정제 산출물(법제처 응답에서 담당자 정보 제거 등) (커밋 안 함)
├── seed/         # 시드 데이터 YAML (커밋)
├── synthetic/    # 생성 문서·메타데이터 (커밋)
└── golden/       # 질의셋·qrels·사람 채점 (커밋, 사람만 수정)
docs/results/     # 실험 결과표·가설 사전 등록 (날짜·커밋 해시 포함)
logs/traces/      # answer() JSONL 트레이스 (커밋 안 함)
infra/            # docker-compose (OpenSearch + nori), fontconfig(HWP 변환 글꼴 대체 규칙)
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
법제처 Open API(현행·연혁 조문·별표 목록) → 정제 JSON (파싱 정답·개정 날짜 기준)
ALIO 사규(HWP/HWPX, 개정 버전) → 버전 매칭 → HWP/HWPX→PDF → 파서 → 조문 분할 → 구조 청킹 ─┐
시드 데이터 YAML → 합성 문서(DOCX/XLSX/MD/HTML/PDF) → Docling → 청킹 ──────────────────┼→ BGE-M3 → OpenSearch (권한·식별자·날짜 메타데이터)
시드 데이터 YAML → 티켓·결재 ────────────────────────────→ SQLite

[질의]
사용자(팀·직급) → answer() ─ 단순 RAG: search_docs 1회 → 답변
                          └ Agentic RAG(LangGraph): 도구 선택 반복(≤5) → 답변(인용 필수)
Streamlit 데모 → answer() (사용자 전환)
외부 클라이언트(B, Claude Code) → MCP(bearer 토큰) → 같은 도구
                          모든 도구 → retrieval.authorized_* → OpenSearch / SQLite
```

## 상태 관리
- **인덱스:** OpenSearch 인덱스는 실험 변형(분석기·청킹)별로 이름을 나눠 만든다. 재생성 가능한 산출물이다.
- **정형 데이터:** SQLite 파일은 시드 데이터에서 재생성한다. `run_sql`은 읽기 전용으로 연다.
- **권한:** 문서 메타데이터 `access = {level: public|team|restricted, teams: [...], min_grade}`. 사용자 컨텍스트는 서버가 결정한다(LangGraph `ToolRuntime` context, MCP bearer 토큰 매핑).
- **식별자:** 적재 시 정규식으로 추출해 분석하지 않는 `keyword` 필드에 저장한다. `lookup_id`는 `term` 쿼리로 정확 일치 조회한다(분석기가 하이픈에서 쪼개는 문제 회피).
- **비밀 값:** API 키는 `.env`에서만 읽는다.

## 트레이스
`answer()`는 질의마다 JSONL 한 줄을 남긴다: 질의 ID, 사용자, 시스템(단순/Agentic/라우터), 단계별(도구·인자·지연·토큰·비용), 구간 지연(임베딩·검색·TTFT·전체), 인용, 거절 여부. 실패 분석·GUI·비용 리포트가 이 파일을 읽는다. 필드는 프로젝트 B에서 Langfuse로 옮기기 쉽게 span 구조를 따른다.

## MCP 계약
- 도구 4개(`search_docs`, `lookup_id`, `run_sql`, `get_document`)는 입출력 스키마와 버전(예: `search_docs` v1)을 가진다. 호환되지 않는 변경은 버전을 올린다.
- 공통 오류 형식: `{error: {code: unauthorized | not_found | invalid_argument | limit_exceeded, message}}`. 권한 없음 응답은 문서의 존재 여부를 드러내지 않는다.
- 인증: 데모 사용자별 정적 bearer 토큰 → 토큰·팀·직급 매핑 파일을 서버가 읽는다. 실서비스는 MCP 명세의 OAuth 2.1로 교체한다(한계).
- 계약 테스트가 스키마 스냅샷과 오류 형식을 검증한다.
