# Step 12: threat-model

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·MCP 계약
- `docs/ADR.md` — ADR-004·010·018·019·029와 (step 1 이후) ADR-030
- `docs/EVAL.md` — 지표·보안·운영·채점 모델 검증
- `docs/results/hypotheses.md` — H5·H6과 'M6 판정 규칙', (step 1 이후) 'M7 판정 규칙' 절
- `docs/ADR.md`의 ADR-009·010·011·018·019·029·030과 `docs/ARCHITECTURE.md`의 '단일 인가 지점'·'MCP 계약'·'권한'·'트레이스'
- `docs/results/hypotheses.md`의 M7-L1~L5, `docs/EVAL.md`의 보안 절
- `src/kodocs/retrieval/access.py`·`sql.py`, `src/kodocs/tools/`, `src/kodocs/mcp_server/`, `data/synthetic/injections.json` — 실제 방어 지점 확인
- `tests/` 아래 문서 링크·ADR 상호참조를 검사하는 기존 테스트(있으면 방식)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 판정 규칙(hypotheses.md의 'M7 판정 규칙', step 1에서 추가)은 결과를 보기 전에 사전 등록한 값이므로 바꾸지 마라. 검색 구성은 M5 최종 구성(`data/manifest/m5_config.json`, config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)과 M6 고정 설정(`data/manifest/m6_config.json`, 답변 모델 gpt-6-luna)으로 고정이며 바꾸지 않는다. 답변은 새로 만들지 않고 M6 산출물(`data/interim/m6/`)을 재사용한다(보안 테스트셋·지연 측정 제외).

## 작업

`docs/THREAT_MODEL.md`를 쓴다. 문서만 쓰는 step이다(코드는 상호참조 검사 테스트뿐). **실측 수치는 싣지 않는다**(결과는 M7 결과 문서에서 채운다). 지표·테스트 이름만 적는다.

### 문서 구성
1. **범위와 가정**: 보호 대상(제한 문서·카나리아·원장 제한 행·데모 토큰), 비목표(운영 인증, OAuth 2.1 — 데모는 정적 토큰, 쓰기 도구 없음), 공격자 모델(권한 없는 임직원, 권한 있는 사용자의 문서에 심긴 인젝션, 외부 에이전트 클라이언트).
2. **신뢰 경계와 데이터 흐름**: 사용자·클라이언트 → `answer()`/MCP(bearer) → `tools.run_tool` → `retrieval.authorized_*`(pre-filter) → OpenSearch·SQLite. 사용자 컨텍스트는 도구 인자가 아니라 서버가 주입하며 모델이 권한 값을 보거나 만들 수 없다(ADR-010). 아키텍처 문서와 같은 경계를 그림(텍스트 다이어그램)으로 보인다.
3. **위협 표**(식별 → 설명 → 대표 시나리오 → 완화 → 검증 지표·테스트 → 잔여 위험): OWASP LLM Top 10 2025의 LLM01(프롬프트 인젝션: 직접·간접), LLM02(민감 정보 노출), LLM06(과도한 에이전시)에 매핑한다. 최소 포함 항목: 직접 질문·우회 표현(관리자 사칭·지시 무시), 식별자 직접 조회·열거, 존재 누설(권한 부족과 없는 ID의 응답 차이: `not_found` 바이트 동일, ADR-018), 간접 프롬프트 인젝션 문서(ADR-019, 검색 전 인가로 후보에 오르지 않음), SQL 우회(`run_sql` SELECT만·허용 테이블·LIMIT·읽기 전용), 부분 답변을 통한 정보 누설(Luna의 부분 답변 경향), 정적 bearer 토큰 탈취·재사용, 트레이스·로그에 제한 문서 내용·토큰 잔존, 문서 메타데이터 오류(권한 모델이 메타데이터에 의존, ADR-010 트레이드오프), 가드레일 소진을 통한 서비스 거부(MAX_STEPS·토큰·타임아웃).
4. **완화 ↔ 검증 매핑**: 각 완화가 어떤 테스트·지표로 확인되는지 표로 적는다 — 보안 테스트셋 누설 판정 (a)(b)(c1~c3), 대조군 권한 오거절률, 도구 계약 테스트·parity, 계층 가드 테스트, 노출률·Agentic 시도율(비판정 보조 지표). H6의 한계(0건은 관측값이며 95% 상한 3/n, 증명 아님)를 명시한다.
5. **잔여 위험·한계**: 인젝션이 권한 밖 유출은 못 해도 답변 품질을 오염시킬 수 있다(ADR-019 트레이드오프), 정적 토큰, 메타데이터 신뢰, 합성 코퍼스 기반 시나리오의 한계, c1 판정이 사람 확인에 의존, 시스템·문항당 1회 실행의 비결정성.
6. **운영 권고(MVP 밖)**: 실서비스 교체 사항(OAuth 2.1, 감사 로그 보존·마스킹, 토큰 회전, 응답 시간 평준화 검토)을 짧게 적는다. 구현하지 않는다.

### 상호참조 테스트
- `tests/test_threat_model_doc.py`: `docs/THREAT_MODEL.md`가 존재하고, 언급한 ADR 번호(`ADR-\d{3}`)가 모두 `docs/ADR.md`에 있으며, 위협 표에 필수 항목 키워드(직접·우회·식별자·존재·간접 인젝션·SQL·부분 답변·토큰·메타데이터)가 모두 있고, 문서에 수치 결과(예: `누설 0/`·`P95` 실측 값)가 들어가지 않았다(정규식 검사).
- `AGENTS.md`는 수정하지 않는다. `docs/ARCHITECTURE.md`에는 문서 링크 한 줄만 추가해도 된다(`docs/THREAT_MODEL.md` 참조).

## Acceptance Criteria

```bash
make verify   # ruff + pytest
uv run pytest -q tests/test_threat_model_doc.py
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. `make verify`가 통과하기 전에는 `completed`를 보고하지 않는다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch·SQLite 직접 호출, BGE-M3 로컬, LangGraph는 조율만)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, 사용자 컨텍스트는 도구 인자 금지, `llm/` 래퍼 경유, golden 불변, 원문 비커밋, 테스트는 LLM·외부 API mock, 평문 토큰 비노출)을 위반하지 않았는가?
   - 위협 표의 모든 완화가 실제 코드·테스트·지표 이름으로 매핑되어 있는가(존재하지 않는 테스트 이름을 지어내지 않았는가)?
   - 측정 수치가 문서에 없는가?
   - H6의 한계(0은 관측값, 3/n 상한)가 적혀 있는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 누설률·P95 등 실측 수치를 문서에 쓰지 마라. 이유: 결과는 M7 결과 문서가 단일 원본이며 이 step은 결과를 보기 전에 실행될 수 있다.
- 존재하지 않는 방어를 있는 것처럼 쓰지 마라. 이유: 위협 모델이 구현과 어긋나면 잔여 위험이 숨는다. 각 완화는 코드와 테스트로 확인한 것만 적는다.
- 코드 변경(방어 추가·권한 로직 수정)을 하지 마라. 이유: 이 step은 문서화이며 권한 로직은 `retrieval.authorized_*` 한 곳이다.
- 실제 LLM·외부 API(OpenAI·Gemini·Anthropic)를 호출하지 마라. 테스트는 mock·fake만 쓴다. 이유: `make verify`는 API 키 없이 통과해야 하며, 실제 호출은 사용자가 직접 실행한다(ADR-014).
- `.env`를 열거나 출력하지 마라. 이유: 비밀 값이 들어 있다.
- `data/golden/`(질의셋·qrels·사람 채점)를 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다. (`data/golden/m7_human/`·`data/golden/m7_leak_review/`는 사람만 작성하는 파일이다. 테스트 fixture는 tmp 경로에서만 만든다.)
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id·점수만 남기고 조문·표·답변·참조 답변 원문 텍스트를 넣지 마라.
- M5·M6 확정 산출물(`data/manifest/m5_*`, `data/manifest/m6_*`, `docs/results/2026-09-30-m5-retrieval.md`의 수치)과 hypotheses.md의 M5·M6 판정 규칙·기존 변경 기록을 수정하지 마라. 이유: 결과를 본 뒤 조건을 바꾸면 사전 등록이 무효가 된다(ADR-026). (step 0이 명시한 M6 결과 문서의 지정 칸만 예외.)
- `src/kodocs/retrieval/`·`src/kodocs/tools/`의 동작을 바꾸지 마라. 이유: 권한 pre-filter와 도구 계약은 측정 대상이며 M5·M6 결과와 계약 스냅샷이 이 코드에 묶여 있다.
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정하고, 커밋은 execute.py가 `make verify` 통과를 직접 확인한 뒤 한다.
- 지시에 없는 네트워크 요청·다운로드·패키지 설치(`uv add` 포함)를 하지 마라. 이유: 사전 승인 대상이다(FP-040). 새 의존성이 필요해 보이면 `blocked`로 보고한다.
- 기존 테스트를 깨뜨리지 마라.
- 요청받지 않은 리팩터링·인접 코드 정리를 하지 마라. 이유: 변경된 모든 줄은 이 step의 목적과 직접 연결되어야 한다.
