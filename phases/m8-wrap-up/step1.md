# Step 1: public-audit

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `src/kodocs/eval/m7_rawhash.py`·`m7_report.py` — manifest·결과 문서 생성 방식(`<!-- BEGIN generated -->` 영역과 '사용자 보존 영역')
- `src/kodocs/mcp_server/tokens.py` — 토큰 생성 방식(`secrets.token_urlsafe(32)`)과 `data/interim/mcp/tokens.json`·`data/manifest/mcp_token_hashes.json` 구조(평문 토큰 형식 확인용, 평문 파일 자체는 열지 않는다)
- `src/kodocs/synth/`의 합성 문서 검증기(실존 기관명 검사 방식·목록 위치)와 `docs/results/2026-09-27-m3-synthetic.md` 검증 절
- `data/interim/law/` 디렉터리 구조(로컬 전용·비커밋, 파일 이름과 필드 이름만 확인), `.gitignore`
- `docs/ADR.md` ADR-002(원문 재배포 금지, 커밋 허용 범위), `docs/THREAT_MODEL.md`
- `Makefile`(`.PHONY` 줄과 `m7-*` 타깃 형식), `tests/test_m5_make_targets.py`·`tests/test_m6_make_targets.py`(Makefile 타깃 테스트 방식)
- `git log --all --format=%ae` 형태로 작성자 이메일 도메인 분포를 로컬에서 확인할 수 있다(값은 이 문서·출력에 쓰지 않는다)

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 이 phase(m8-wrap-up) 개요

PRD M8: 공개 가능한 마무리(공개 전환 점검, ADR 정리, Streamlit GUI, README)와 리랭커 실험(H8). 속도 우선이라 필수 항목(step 0~4)을 먼저 끝내 언제든 공개할 수 있는 상태를 만들고, 리랭커(step 5~7)는 그 뒤에 붙인다.

| step | name | 내용 |
|---|---|---|
| 0 | `m8-prereg` | H8 사전 등록(hypotheses.md 'M8 판정 규칙', ADR-031)과 규칙 절 해시 모듈 |
| 1 | `public-audit` | 공개 전환 점검 스캐너(`make m8-audit`·`make m8-audit-gh`)와 점검 문서 |
| 2 | `docs-cleanup` | ADR 상태·색인, M7 정정(패키지명·parse_error 오프라인 분류), 별표 한계를 M8 결과 문서로 |
| 3 | `streamlit-ui` | Streamlit 데모 GUI(질의 화면·결과표 뷰어), answer()만 호출 |
| 4 | `readme` | README 생성기(`make readme`)와 README.md. H8은 결과가 없으면 '미실행' |
| 5 | `rerank-core` | 리랭커 상수·CrossEncoder 래퍼·순서 재배열 순수 함수·`make rerank-models` |
| 6 | `rerank-dev` | 후보·점수 캐시, max_length 확정, dev 격자, freeze |
| 7 | `rerank-test` | test 1회 잠금, 대조군 일치 검사, 짝 비교 부트스트랩, 지연, 결과 문서 |

고정 전제(모든 step 공통):
- M5~M7 확정 결과와 고정 설정(m5 `config_hash` `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`, `data/manifest/m6_config.json`, `data/manifest/m7_config.json`)은 소급 변경하지 않는다. M8 실험은 새 구성으로 따로 기록하고 기존 판정과 섞지 않는다.
- M8 결과 문서 파일명 날짜는 고정값 `2026-10-03`이다: `docs/results/2026-10-03-m8-wrap-up.md`(step 2), `2026-10-03-m8-public-readiness.md`(step 1), `2026-10-03-m8-rerank.md`(step 7).
- 이 phase의 step에서는 실제 API 호출, 모델·데이터 다운로드, 패키지 설치, `.env` 열람을 하지 않는다. 사용자가 직접 실행하는 명령은 step 파일이 아니라 사용자가 실행한다.

## 작업

저장소를 공개해도 되는지 판단할 근거(체크리스트)를 **결정론으로** 만든다. 공개 여부·이력 처리·LICENSE의 최종 결정은 사용자가 한다. 스캐너는 판정하지 않고 건수와 위치만 낸다.

모듈은 `src/kodocs/eval/m8_audit.py`(로컬 점검)와 `src/kodocs/eval/m8_audit_gh.py`(GitHub 점검)이며, 결과 manifest는 `data/manifest/m8_audit.json`·`data/manifest/m8_audit_gh.json`, 문서는 `docs/results/2026-10-03-m8-public-readiness.md`다.

### 1. 출력 원칙(가장 중요)
- 스캐너는 **매치된 값(키 문자열, 본문 구절, 이메일 로컬 파트)을 어떤 출력·manifest·문서·예외 메시지·로그에도 쓰지 않는다.** 쓰는 것은 패턴 이름·경로·건수·최대 길이뿐이다. 이유: 점검 도구가 비밀을 다시 유출하는 사고를 막는다(FP-020의 '정제가 원문을 바꿈'과 같은 계열, AGENTS.md의 키·토큰 비출력 규칙). 테스트: 가짜 비밀값이 든 임시 git 저장소를 만들어 스캔한 뒤 stdout·stderr·manifest·문서·발생시킨 예외 문자열 어디에도 가짜 값이 없음을 검사한다.
- 점검은 `git`을 읽기 전용으로만 호출한다(`ls-files`, `rev-list`, `cat-file`, `log`, `for-each-ref`, `remote get-url`). 쓰기 명령(`add`·`commit`·`gc`·`filter-*`)을 호출하지 않는다. 테스트로 허용 명령 목록을 강제한다.
- 판정하지 않는다: 종료 코드는 도구 오류(git 실행 실패, 읽을 수 없는 입력)일 때만 0이 아니다. 발견 건수가 있어도 0이다.

### 2. 로컬 점검 `make m8-audit` (네트워크 없음)
대상은 (가) 작업 트리 추적 파일 (나) `git rev-list --objects --all`의 모든 blob(blob SHA로 중복 제거, 5MB 초과 blob은 건너뛰고 건수 기록, 디코드 불가 blob은 건수만 세고 계속한다 — FP-030). 스캔한 blob이 0개이면 오류로 멈춘다(FP-031: 0건이 성공으로 끝나면 안 된다).
1. **키 패턴**: Anthropic(`sk-ant-`), OpenAI(`sk-` 긴 토큰), Google(`AIza`), 업스테이지, 법제처 `OC=` 값이 비어 있지 않은 쿼리, `api_key|secret|token`류 대입문, MCP 평문 토큰 형식(`token_urlsafe(32)` = 43자 URL-safe 문자열이 토큰 키 근처에 있는 경우)을 패턴 이름별 건수·경로로 센다. 패턴은 상수 표 한 곳에 두고 테스트로 각 패턴이 의도한 가짜 값만 잡는지 확인한다(정상 문서의 긴 해시·UUID를 잡지 않는다).
2. **경로**: 이력에 한 번이라도 있었던 경로 중 `.env`(`.env.example` 제외)·`data/raw/`·`data/interim/`·`tokens.json`·HWP/HWPX/PDF/SQLite 확장자·`data/golden/tables/`(README 제외)가 있는지, 현재 트리에 있는지 따로 센다.
3. **개인정보 패턴**: 전화번호 형식, 담당자명·전화번호 필드명(`data/manifest`·`data/interim` 규칙 ADR-002), 절대 경로(`/Users/` 등)를 경로별 건수로 센다.
4. **합성 문서의 실존 기관명**: `data/synthetic/` 아래 커밋된 파일에서 합성 문서 검증기가 쓰는 기관명 목록과 같은 목록으로 건수를 센다(목록은 검증기 코드에서 import하거나 같은 상수를 쓴다. 새로 추측해 만들지 않는다).
5. **작성자·커미터 이메일**: `git log --all`의 author·committer 이메일을 **도메인 단위로 묶어** 도메인 이름과 건수만 낸다(로컬 파트 출력 금지). 개인 메일 서비스 도메인과 그 밖의 도메인을 구분해 센다.
6. **규정 원문 슁글 겹침**: 로컬 `data/interim/law`(그리고 점검에 쓸 수 있는 파서 산출 텍스트가 있으면 그 경로)의 조문 텍스트를 NFC·공백 제거 후 30자 슬라이딩 윈도 해시 집합으로 **메모리에만** 만든다(디스크·manifest에 쓰지 않는다). 커밋된 텍스트 파일(작업 트리 + 이력 blob)마다 **겹친 슁글 수와 연속 겹침 최대 길이(문자 수)**만 낸다. 법령 상투구 때문에 오탐이 날 수 있으므로 판정하지 않는다. 로컬 원문 디렉터리가 없으면 `status="skipped: 로컬 원문 없음"`으로 명시하고 통과로 세지 않는다(FP-031). 사용한 원문 소스 경로 목록(파일 수만)을 manifest에 남긴다.
7. **커밋 해시 참조 인벤토리**(이력 정리를 하면 깨지는 곳): 손으로 적지 말고 코드가 생성한다. (가) `src/` 아래 `rev-parse`를 호출하는 파일·줄을 grep으로 모아 목록과 건수를 낸다(현재 9개 파일 11곳으로 예상되지만 테스트에 이 숫자를 박지 마라 — 실제 grep 결과를 쓴다). (나) `data/manifest/*.json`·`docs/results/*.md`에서 기록된 커밋 해시를 센다. 센 대상은 **서로 다른 (파일, 해시) 쌍**이고, 해시는 `커밋|commit`이 들어간 JSON 키의 값 또는 같은 줄에 `커밋|commit|code_commit`이 있는 7~40자 소문자 16진 토큰이다(정의를 manifest에 적는다 — FP-043). 각 해시가 현재 저장소에 존재하는지(`git cat-file -e`)도 건수로 낸다. 이 인벤토리는 문서의 "이력 정리 대안(`filter-repo --mailmap` + old→new SHA 대응표 + 문서 내 해시 갱신)의 비용" 절에 대상 파일 수·해시 수로 쓴다. 이 대안을 실행하는 step·스크립트는 만들지 않는다.
8. **저장소 위생**: `LICENSE` 존재 여부, `README.md` 존재 여부, `.env.example` 존재 여부, 이력에서 `.env`가 추가된 적이 있는지.
9. **ref 개수**: `git for-each-ref`로 로컬이 아는 ref 종류별 개수. (GitHub의 `refs/pull/*`는 로컬에 없을 수 있으므로 GitHub 점검에서 PR 개수를 센다.)

### 3. GitHub 점검 `make m8-audit-gh` (사용자 실행, 네트워크)
- `gh api`의 **GET 읽기 호출만** 쓴다: 이슈·PR 본문, 이슈 댓글, PR 리뷰 댓글, PR 리뷰 본문, 이슈·PR 개수, PR 개수(= 공개 시 보이는 `refs/pull/*` 개수 근거). owner/repo는 `git remote get-url origin`에서 파싱한다(하드코딩 금지). 테스트에서 subprocess를 mock해 호출 인자에 `POST|PATCH|PUT|DELETE`, `-f`, `-F`, `--input`이 없고 `--method`가 있으면 GET뿐임을 강제한다.
- 각 본문에 위 키 패턴·개인정보 패턴·슁글 겹침을 적용하고 출력은 (종류, 번호, 패턴 이름, 건수, 슁글 겹침 수·최대 길이)뿐이다. 본문 텍스트·작성자 이름을 출력하지 않는다. 페이지네이션 중 항목 하나의 실패는 건수로 세고 계속한다(FP-030).
- step에서는 이 타깃을 실행하지 않는다(네트워크). 테스트는 mock으로만 한다.

### 4. 결과 manifest·문서
- `data/manifest/m8_audit.json`: 실행 시각, HEAD 커밋, 스캔한 blob·파일 수, 위 1~9의 건수·경로·최대 길이(값 없음), 제외·건너뜀 건수와 사유. 문서와 manifest는 `make m8-audit`를 다시 실행하면 덮어쓴다(이 점검 결과는 커밋 이력에 따라 달라지는 시점 값이다).
- `docs/results/2026-10-03-m8-public-readiness.md`: `<!-- BEGIN generated -->` 영역(manifest에서 생성, 손으로 옮기지 않음)과 아래 '사용자 결정' 영역을 둔다. 사용자 결정 영역은 체크리스트 항목과 기본안만 담고 결정 칸은 비워 둔다:
  - [ ] 공개 여부(사용자 결정)
  - [ ] 이력 처리 — **기본안: 이력 유지.** 이유: 결과 문서·manifest에 기록된 커밋 해시(예: `m5_config.code_commit`, M7 측정 코드 커밋, `rev-parse`로 해시를 기록하는 코드 경로)가 이력 재작성 후 없는 커밋을 가리키게 되고, 사전 등록(결과를 보기 전에 규칙을 커밋했다는 시간순)의 근거가 깨진다. 새 저장소(스쿼시)도 같은 이유로 뺀다. 대안(회사 메일을 반드시 지워야 하는 경우만): `git filter-repo --mailmap` + old→new SHA 대응표 + 문서 내 해시 갱신. 비용은 위 인벤토리의 대상 파일 수·해시 수로만 적는다(생성 영역 값을 가리킨다).
  - [ ] 작성자 이메일 — 도메인·건수는 생성 영역. liveuou.kr 21건은 GitHub 웹 머지 커밋(계정 이메일)이고 회사 메일 도메인은 5건이라는 사용자 설명을 사실로 적되 스캐너 값과 일치하는지 생성 영역과 대조하는 칸을 둔다. 앞으로의 커밋 이메일(noreply) 설정은 사용자가 GitHub에서 직접 한다(항목만).
  - [ ] LICENSE — 코드는 MIT를 기본 후보로 둔다. 범위 문장을 함께 둔다: "합성 데이터·문서는 같은 라이선스, 실제 규정 원문은 저장소에 없음". 최종 결정은 공개 직전에 사용자가 한다. 이 step은 LICENSE 파일을 만들지 않는다.
  - [ ] 슁글 겹침 검토 — 건수·최대 길이가 큰 파일은 사용자가 직접 검토(스캐너는 판정하지 않음).
  - [ ] GitHub 이슈·PR·댓글 점검 결과(`make m8-audit-gh`) 확인. 공개하면 `refs/pull/*`도 함께 보인다는 점 확인.
  - [ ] 공개 직전 `make m8-audit` 재실행(이후 커밋이 점검 대상에 들어간다).
- 각 항목 아래에 '해당 절 위치'나 근거 문서 링크를 적되 값은 쓰지 않는다.

### 5. Makefile·AGENTS.md
- `.PHONY` 줄에 `m8-audit m8-audit-gh`를 추가하고 타깃을 만든다: `m8-audit`는 `uv run python -m kodocs.eval.m8_audit`, `m8-audit-gh`는 `uv run python -m kodocs.eval.m8_audit_gh`.
- `AGENTS.md` `## 명령어`에 한 줄씩 추가한다: `make m8-audit  # 공개 전환 점검(작업 트리·git 이력·슁글 겹침·이메일 도메인·해시 인벤토리) → data/manifest/m8_audit.json·docs/results/2026-10-03-m8-public-readiness.md — 네트워크 없음`, `make m8-audit-gh  # GitHub 이슈·PR·댓글 점검(gh 읽기 전용, 건수·위치만) — 네트워크, 사용자 실행`.

### 6. 이 step에서 실행하는 것
구현 후 로컬에서 `make m8-audit`를 한 번 실행해 결과 manifest·문서를 만든다(네트워크 없음). 결과에 비밀값이 있는지 눈으로 확인하지 말고 테스트가 보장하게 한다. 건수가 있으면 최종 보고 summary에 패턴 이름별 건수만 적는다(값 금지).

## Acceptance Criteria

```bash
make verify   # ruff + pytest 통과
make m8-audit   # 로컬 점검 실행(네트워크 없음), manifest·문서 생성
make -n m8-audit-gh   # 타깃이 선언돼 있는지만 확인(실행하지 않는다)
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

- 스캐너 출력·manifest·문서·예외 메시지에 매치된 비밀값·본문 구절·이메일 로컬 파트를 쓰지 마라. 이유: 점검 도구가 비밀을 다시 퍼뜨리는 사고를 막는다(FP-020 계열). 이메일은 도메인과 건수만 쓴다.
- `make m8-audit-gh`를 실행하거나 `gh`를 호출하지 마라. 이유: 네트워크 호출은 사용자 실행이다(FP-040). GitHub에 쓰기(POST·PATCH·DELETE·`-f`)는 어떤 경우에도 하지 마라.
- git 이력을 바꾸는 명령(`filter-repo`·`rebase`·`commit --amend`·`gc --prune`·`reset`)을 호출하거나 그런 스크립트를 만들지 마라. 이유: 이력 정리는 기본안에서 제외됐다(커밋 해시를 기록한 manifest·결과 문서가 깨진다). 대안은 비용만 문서에 적는다.
- LICENSE·README 파일을 만들지 마라. 이유: LICENSE는 사용자가 공개 직전에 결정하고 README는 step 4가 만든다.
- 공개 가능 여부를 스캐너가 '통과/실패'로 판정하게 하지 마라. 이유: 슁글 겹침은 법령 상투구 오탐이 있어 사용자가 검토한다. 로컬 원문이 없으면 '건너뜀'이지 통과가 아니다(FP-031).
- 해시 인벤토리의 건수(11곳·9개 파일 등)를 테스트·문서에 손으로 박지 마라. 이유: 코드가 grep으로 생성한 값만 쓴다(손으로 옮기면 어긋난다).
- 실제 비밀값·실제 규정 원문을 테스트 fixture에 넣지 마라. 이유: 가짜 값(패턴만 맞는 임의 문자열)과 임시 git 저장소만 쓴다.
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 6·7이 만드는 `data/interim/m8/` 캐시 쓰기만 허용되며 이 경로도 커밋하지 않는다.
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
