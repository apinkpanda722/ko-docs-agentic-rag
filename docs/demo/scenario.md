# 데모 촬영 대본 (수동 절차)

주 데모는 Claude Code를 MCP 클라이언트로 붙인 장면이고, 보조 데모는 Streamlit 질의 화면이다(ADR-021, ADR-032). 실행 스크립트·launcher는 만들지 않는다. 아래 절차를 사용자가 직접 실행한다. **토큰 값은 화면·로그·문서에 절대 나오지 않게 한다(`echo $KODOCS_MCP_TOKEN` 금지).**

실제 호출은 과금되는 OpenAI 호출을 포함한다. 촬영 전에 `make m8-stream-repro-dry`처럼 예상 비용을 확인하는 명령은 없으므로 장면당 질문 수를 대본대로만 쓴다.

## (가) 준비

### 1. 서버 (터미널 1, 저장소 루트)

```bash
make up          # OpenSearch 기동
make m6-serve    # MCP 서버 127.0.0.1:8765/mcp (BGE-M3 적재가 끝날 때까지 기다린다)
```

### 2. 토큰 없이는 401임을 확인

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 기대 출력: 401
```

### 3. 클라이언트 폴더 준비 (저장소 밖, 1회만)

```bash
mkdir -p ~/kodocs-demo && cd ~/kodocs-demo
claude mcp add -s project --transport http kodocs http://127.0.0.1:8765/mcp \
  --header 'Authorization: Bearer ${KODOCS_MCP_TOKEN}'
```

- 작은따옴표 때문에 설정 파일(`.mcp.json`)에는 토큰 값이 아니라 **변수 이름만** 저장된다.
- 이미 있으면 "already exists"가 나오며 정상이다.

### 4. 사용자별 토큰 주입 (값 비출력)

```bash
export KODOCS_MCP_TOKEN="$(python3 -c 'import json,sys; users=json.load(open(sys.argv[1]))["users"]; print(next(u["token"] for u in users if u["label"]==sys.argv[2]))' \
  /절대경로/ko-docs-agentic-rag/data/interim/mcp/tokens.json demo-platform-g3)"
echo ${#KODOCS_MCP_TOKEN}     # 43이면 정상(길이만 출력)
```

- 마지막 인자가 라벨이다(`demo-<팀>-g<직급>`). 터미널마다 라벨을 바꿔 실행한다.
- 토큰 파일(`data/interim/mcp/tokens.json`)은 gitignore된 로컬 파일이다. 열거나 화면에 띄우지 않는다.

### 5. Claude Code 실행

```bash
cd ~/kodocs-demo
claude --model sonnet --no-chrome --strict-mcp-config --mcp-config .mcp.json \
  --tools "" --allowedTools mcp__kodocs
```

| 옵션 | 이유 |
|---|---|
| `--strict-mcp-config --mcp-config .mcp.json` | 다른 MCP 서버를 제외한다 |
| `--no-chrome` | 내장 Chrome MCP를 제외한다 |
| `--tools ""` | 내장 도구(파일 읽기·Bash)를 막아 저장소 파일을 직접 읽는 우회를 방지한다 |
| `--allowedTools mcp__kodocs` | kodocs 도구 승인 프롬프트를 생략한다 |

- `/mcp`에 **kodocs 1개, 도구 4개**(`search_docs`, `lookup_id`, `run_sql`, `get_document`)만 보이면 정상이다. 촬영 화면에서는 `/mcp` 상세 화면을 열지 않는다.
- **사용자 전환은 Claude Code를 종료한 뒤 `export`를 바꿔 다시 실행**한다(`/clear`로는 바뀌지 않는다).
- 촬영은 터미널을 좌우로 분할해 두 사용자가 동시에 접속한 상태로 한다(왼쪽 `demo-platform-g3`, 오른쪽 `demo-finance-g3`).

## (나) 장면 2 — Claude Code 질문과 기대값

기대값은 조언 세션이 사전에 조회한 값이다. 촬영 전 같은 질문으로 한 번 확인한다.

| # | 질문 | 왼쪽 demo-platform-g3 | 오른쪽 demo-finance-g3 |
|---|---|---|---|
| 1 | "INC-2026-012 장애의 원인과 조치를 찾아서 세 줄로 요약해줘. 근거 문서 ID도 붙여줘." | `syn-incident-022`(플랫폼팀 전용, 이 ID를 언급하는 유일한 문서) 근거 답변 | 찾을 수 없음(공개 티켓 PLT-1004 제목까지만 보일 수 있음) |
| 2 | "INC-2026-099 장애도 같은 방식으로 찾아줘." | 찾을 수 없음(원장·문서 어디에도 없는 번호) | 찾을 수 없음 |
| 3 | (오른쪽만) "syn-incident-022 문서를 열어줘." | — | `get_document` → `not_found`. Ctrl+O로 위 두 응답(2번·3번)이 같은 모양임을 보여 준다 |
| 4 | "플랫폼팀(owner_team이 platform) 티켓을 상태별로 세고 합계도 알려줘." | `run_sql`, 합계 **94** | `run_sql`, 합계 **56**(공개분만) |

## (다) 장면 3 — Streamlit (`make ui`, 질의 화면만)

- 시스템은 **agentic**. "같은 질문을 두 사용자로 실행"을 누른다(A 완료 후 B 실행, 편집에서 B 대기 구간을 배속한다). 결과표 탭은 없다.
- **직급 차이**: A `demo-hr-g4` / B `demo-hr-g2`, "인사 평가 조정 작업을 시작하기 전에 점검할 항목을 알려줘" → A는 제한 문서 `syn-runbook-026` 근거, B는 그 문서가 나오지 않는다.
  - 리허설에서 B의 검색 결과에 실제 규정 문서(`law-…`)가 섞였다. **촬영 전 B 답변 본문에 실제 규정 내용이 인용되지 않는지 확인한다**(확인 항목).
- **SQL**: A `demo-platform-g3` / B `demo-finance-g3`, 위 SQL 질문 → **94 / 56**(근거에 실행 SQL이 포함되도록 고친 뒤에는 거절 없이 답변해야 한다).
- 선택: 같은 SQL 질문을 시스템 **simple**로 실행하면 집계가 되지 않는다(단순 RAG에는 `run_sql`이 없다).

## (라) 장면 구성과 내레이션

| 장면 | 길이 | 화면 | 말 |
|---|---|---|---|
| 0 제목 | 5초(편집 슬라이드) | "ko-docs-agentic-rag" / "권한을 지키면서 질문 유형마다 맞는 도구를 고르는 한국어 사내 문서 Agentic RAG" / 하단 "OpenSearch · BGE-M3 · LangGraph · MCP · 사전 등록 평가" | "한국어 사내 문서에서, 권한을 지키면서 질문마다 맞는 도구를 고르는 Agentic RAG입니다." |
| 1 문제 | 15초(편집 슬라이드) | 왼쪽 "질문마다 필요한 도구가 다르다"(원인 질문→`search_docs`, INC-2026-012→`lookup_id`, 팀 티켓 몇 건→`run_sql`, 문서 전체→`get_document`) / 오른쪽 "같은 문서, 볼 수 있는 사람이 다르다"(INC-2026-012 장애보고서[플랫폼팀 전용]: 플랫폼팀 열람, 재무팀 "없는 문서와 같은 응답") | "사내 문서 질문은 종류가 섞여 있습니다. 본문을 찾는 질문, 장애 번호 같은 식별자를 짚는 질문, 티켓 수를 세는 집계 질문은 각각 다른 도구가 필요합니다. 그리고 같은 문서라도 팀과 직급에 따라 볼 수 있는 사람이 다릅니다." |
| 2 Claude Code + MCP | 70~80초 | 장면 2의 질문 1~4(좌우 분할) | ① "Claude Code를 MCP 클라이언트로 붙입니다. 왼쪽은 플랫폼팀, 오른쪽은 재무팀 토큰입니다. 팀과 직급은 서버가 토큰으로 정하고, 도구 인자로는 넘길 수 없습니다. 파일을 직접 읽지 못하도록 Claude Code의 내장 도구는 껐습니다." ② "같은 장애 번호를 물으면 플랫폼팀은 장애보고서를 근거로 답하고, 재무팀은 공개 티켓 제목까지만 보일 뿐 보고서는 찾지 못합니다." ③ "없는 번호를 묻거나 권한 없는 문서를 ID로 직접 열어도 응답이 똑같습니다. 권한 없는 문서가 존재한다는 사실조차 드러나지 않습니다." ④ "집계 질문에는 SQL 도구를 씁니다. 같은 SQL이지만 행 단위 권한이 적용돼 플랫폼팀은 94건, 재무팀은 공개된 56건만 셉니다." |
| 3 Streamlit | 45초 | 장면 3의 질의 화면 | "GUI는 우리가 만든 에이전트를 같은 도구와 같은 권한 함수로 돌립니다. 데모에서는 사용자를 고르지만 권한 판정은 서버 함수가 합니다. 같은 인사팀 안에서 직급만 다르면 4급은 제한 문서를 근거로 답하고, 2급에게는 그 문서가 나오지 않습니다. 도구 호출과 답변은 생성되는 대로 실시간으로 표시됩니다." |
| 4 결과 | 30초(편집 슬라이드) | nori Recall@10 0.689→0.794 / 리랭커 NDCG@10 0.718→0.782(+0.064 [0.022, 0.116]) / Agentic 대 단순 정답성 +1.615 [0.923, 2.308], SQL 3.0 대 0.0 / 권한 누설 0/200, MCP 포함 4경로 / 한계: 리랭커 P95 3.26초로 서빙 미통합, 판정 보류 3건 공개 | "검색은 한 번에 한 변수씩 바꾸며 측정했습니다. 한국어 형태소 분석기로 Recall이 0.689에서 0.794로, 리랭커로 NDCG가 0.718에서 0.782로 올랐습니다. Agentic RAG는 단순 RAG보다 정답성이 평균 1.6점 높았고, SQL 질문은 0점 대 3점이었습니다. 권한 누설은 MCP를 포함한 네 경로에서 200건씩 시도해 0건이었습니다. 리랭커는 지연이 P95 3.3초라 아직 서빙에는 넣지 않았습니다." |
| 5 작업 방식 | 15초(실화면) | `hypotheses.md` 판정 규칙 절과 SHA-256 → GitHub PR 목록 또는 `phases/` step 파일 → `make verify` 통과 줄 | "가설과 판정 규칙은 결과를 보기 전에 등록하고 해시로 고정했습니다. 구현은 단계별로 나눠 코딩 에이전트에 맡기고, 매 단계 2천여 개 테스트로 검증한 뒤 커밋했습니다. 판정 보류와 기각도 그대로 공개했습니다." |
| 6 끝 | 5초(편집) | 저장소 URL, 2쪽 요약 PDF, 연락처 | — |

장면 4의 수치는 README 핵심 결과(manifest에서 생성)와 같은 값이다. 촬영 전에 README와 어긋나지 않았는지 확인한다.

## (마) 촬영 체크리스트

- [ ] 방해 금지 모드 켜기, Dock 숨김, 바탕화면 정리
- [ ] 터미널 글꼴 18pt 이상, 프롬프트는 짧게
- [ ] 브라우저는 새 프로필, 125% 확대
- [ ] `.env` 등 민감 파일이 열린 창이 없는지, 토큰 값이 화면에 나오지 않는지
- [ ] `/mcp` 상세 화면은 열지 않기
- [ ] macOS Cmd+Shift+5로 전체 화면 무음 녹화
- [ ] 장면당 2~3 테이크, 파일명 `a-s<장면>-take<n>.mov`
- [ ] 메모리가 부족하면 장면 2 촬영 후 MCP 서버(`make m6-serve`)를 종료하고 `make ui`를 실행
- [ ] 장면 3 직급 차이: B 답변 본문에 실제 규정 내용이 인용되지 않는지 확인

영상 링크는 촬영 후 README 데모 절에 추가한다.
