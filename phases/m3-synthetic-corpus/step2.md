# Step 2: ledger-db

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — `run_sql` 규칙(SELECT·허용 테이블·LIMIT·읽기 전용), 권한 pre-filter
- `docs/ADR.md` — ADR-005(SQLite), ADR-010(권한은 검색 전 한 곳에서), ADR-024(SQLite 결정론·권한 열·비커밋)
- `docs/ARCHITECTURE.md` — 상태 관리(정형 데이터는 시드에서 재생성, `access` 형식)
- `docs/EVAL.md` — SQL 집계 문항(SQL 실행 정확도)
- step 0 산출물: `src/kodocs/synth/seed.py`(`Access`, `TEAMS`, `GRADES`, `load_seed`), `src/kodocs/synth/identifiers.py`
- step 1 산출물: `data/seed/ledger/rules.yaml`, `data/seed/ledger/tickets_anchor.yaml`, `data/seed/ledger/approvals_anchor.yaml`, `data/seed/people/`, `data/seed/services.yaml`, `tests/synth/test_seed_committed.py`
- `src/kodocs/ingest/manifest.py` — `append_record`, `sha256_file`, `now_iso`

## 배경

티켓 500행·결재 150행을 **LLM 없이 시드에서 결정론적으로** SQLite로 만든다. 문서가 언급하는 앵커 행은 시드 그대로, 나머지 배경 행은 `rules.yaml`과 `company.yaml`의 `random_seed`로 만든다. M4가 SQL 집계 문항의 정답을 이 DB에서 계산하고, M5·M6의 `run_sql`이 **행 단위 권한 pre-filter**(ADR-010)를 걸 수 있도록 행마다 소유 팀과 공개 범위를 둔다.

## 작업

TDD: `tests/synth/test_db.py`를 먼저 작성한다. 작은 fixture 시드(step 0의 `tests/synth/fixtures/seed_min/`에 ledger 파일을 추가하거나 별도 fixture)와 커밋 시드 양쪽으로 테스트한다.

### 1. `src/kodocs/synth/db.py`

```python
DB_PATH = Path("data/synthetic/ledger.sqlite")          # 커밋하지 않는다(.gitignore에 추가)
MANIFEST_PATH = Path("data/manifest/synth_db.json")      # 커밋한다
SCHEMA_VERSION = 1
def build_rows(seed: Seed) -> dict[str, list[dict]]: ...   # 테이블명 → 행(결정론, 정렬된 순서)
def write_db(rows: Mapping[str, Sequence[Mapping]], path: Path) -> None: ...   # 임시 파일에 쓰고 replace
def content_hash(rows) -> str: ...   # 행 내용의 정규 JSON SHA-256 (SQLite 파일 바이트 해시는 쓰지 않는다)
def main(argv=None) -> int: ...      # python -m kodocs.synth.db
```

**테이블(열 이름·타입 고정)**

- `teams(team TEXT PK, name_ko TEXT, project_key TEXT)`
- `people(person_id TEXT PK, name TEXT, team TEXT, grade INTEGER, grade_name TEXT, role TEXT)`
- `services(service TEXT PK, owner_team TEXT, description TEXT)`
- `tickets(ticket_id TEXT PK, project_key TEXT, owner_team TEXT, title TEXT, ticket_type TEXT, status TEXT, priority TEXT, service TEXT, incident_id TEXT NULL, reporter_id TEXT, assignee_id TEXT, created_at TEXT, resolved_at TEXT NULL, story_points INTEGER NULL, access_level TEXT, access_teams TEXT, access_min_grade INTEGER, is_anchor INTEGER, fact_ids TEXT)`
- `approvals(approval_id TEXT PK, owner_team TEXT, category TEXT, title TEXT, amount_krw INTEGER, status TEXT, requester_id TEXT, approver_id TEXT, submitted_at TEXT, decided_at TEXT NULL, regulation_ref TEXT NULL, access_level TEXT, access_teams TEXT, access_min_grade INTEGER, is_anchor INTEGER, fact_ids TEXT)`
- `access_teams`는 팀 코드를 `,`로 이은 문자열(공개면 빈 문자열), `fact_ids`는 앵커 행의 fact_id를 `,`로 이은 문자열(배경 행은 빈 문자열). 날짜·시각은 ISO 8601(KST, `YYYY-MM-DDTHH:MM:SS+09:00`).
- 외래 키: `people.team`, `tickets.owner_team`·`reporter_id`·`assignee_id`·`service`·`incident_id`(장애 ID는 시드 장애 목록), `approvals.requester_id`·`approver_id`. `PRAGMA foreign_keys=ON`으로 만든다(`incidents` 테이블이 필요하면 `incidents(incident_id PK, service, severity, started_at, resolved_at, owner_team)`를 추가해도 된다).

**배경 행 생성 규칙**

- `random.Random(company.random_seed + 고정 오프셋)` 하나만 쓴다. 전역 `random`·현재 시각·`uuid`를 쓰지 않는다. 이유: 두 번 빌드한 결과가 같아야 M4 SQL 정답이 고정된다.
- 행 수(FP-043: 앵커 포함 전체 행 수를 센다): tickets **정확히 500**, approvals **정확히 150**.
- 권한 분포: `rules.yaml`의 값 그대로(tickets public 300·team 175·restricted 25, approvals public 30·team 90·restricted 30). 앵커 행의 `access_level`을 먼저 반영하고 나머지를 배경 행으로 채운다. 앵커만으로 한 수준의 목표를 넘으면 `SeedError`로 멈춘다.
  - `team`: `access_teams = owner_team`, `access_min_grade = 1` / `restricted`: `access_teams = owner_team`, `access_min_grade = 4` / `public`: `""`, `1`
- 배경 티켓 ID: 팀 프로젝트 키별로 앵커가 쓰지 않은 번호를 `1001`부터 증가하며 부여(앵커 번호와 충돌 금지). 결재 ID: 연도별 `EXP-YYYY-NNNN`, 앵커 번호를 피해 `0001`부터.
- 날짜: 기간(2025-01-01~2026-06-30) 안. `resolved_at >= created_at`, `decided_at >= submitted_at`, 상태가 `open`/`submitted`면 해결·결정 시각 NULL.
- 담당자·요청자는 소유 팀 인물, 결재자는 소유 팀 팀장(grade 5) 또는 금액 상한(`rules.yaml`) 초과 시 `finance` 팀장.
- 자유 서술(`title`)은 **템플릿**으로 채운다(유형·서비스·카테고리별 문장 틀 + 시드 값). LLM을 쓰지 않는다.
- 카나리아가 DB에 새지 않게 한다: 이 step은 카나리아를 모른다(step 3이 만든다). 대신 `title`은 템플릿·시드 값에서만 만든다.

**manifest** (`data/manifest/synth_db.json`, 결정론 JSON, 매 빌드 덮어씀)

`{"schema_version", "db_path", "content_hash", "row_counts": {...}, "access_counts": {"tickets": {"public":300,...}, "approvals": {...}}, "anchor_counts", "random_seed", "seed_files_sha256": {경로: 해시}}` — 빌드 시각은 넣지 않는다(결정론 유지).

### 2. Makefile·.gitignore·AGENTS.md

- `build-db: uv run python -m kodocs.synth.db` (`.PHONY` 추가)
- `.gitignore`에 `data/synthetic/ledger.sqlite` 추가
- AGENTS.md 명령어 절: `make build-db     # 시드 → 티켓·결재 SQLite(data/synthetic/ledger.sqlite, 비커밋) 결정론 빌드, 해시·행 수는 data/manifest/synth_db.json (네트워크 없음)`
- 이 step 안에서 `make build-db`를 한 번 실행해 `data/manifest/synth_db.json`을 만든다.

### 3. 테스트 (필수)

- 결정론: 커밋 시드로 `build_rows` 두 번 → `content_hash` 동일. 서로 다른 임시 경로에 `write_db` 두 번 → 테이블 내용 동일.
- 행 수: tickets 500, approvals 150 정확히.
- 권한 행 수: `SELECT access_level, COUNT(*) ... GROUP BY` 결과가 tickets `{public:300, team:175, restricted:25}`, approvals `{public:30, team:90, restricted:30}`. restricted 행은 모두 `access_min_grade = 4`이고 `access_teams = owner_team`.
- 앵커 행이 시드 값 그대로 들어감(`PAY-1234`, `EXP-2026-0412` 조회), `fact_ids`가 시드 fact_id.
- 식별자 형식(`TICKET_RE`·`APPROVAL_RE` 전체 일치), 프로젝트 키 = 소유 팀 키, 날짜 기간 안, 시각 순서, FK 무결성(`PRAGMA foreign_key_check` 빈 결과), ID 중복 없음.
- 앵커가 한 권한 수준 목표를 넘는 fixture → `SeedError`.
- `data/manifest/synth_db.json`의 `content_hash`가 커밋 시드로 다시 계산한 값과 같다(시드를 고치고 `make build-db`를 잊으면 `make verify`가 실패하게).

## Acceptance Criteria

```bash
make verify
make build-db
sqlite3 data/synthetic/ledger.sqlite "SELECT access_level, COUNT(*) FROM tickets GROUP BY 1 ORDER BY 1; SELECT access_level, COUNT(*) FROM approvals GROUP BY 1 ORDER BY 1;"
git check-ignore data/synthetic/ledger.sqlite
```

## 검증 절차

1. 위 AC 커맨드를 실행한다. 권한별 행 수가 tickets public 300·restricted 25·team 175, approvals public 30·restricted 30·team 90인지 확인한다.
2. 아키텍처 체크리스트를 확인한다:
   - 전역 `random`·현재 시각·`uuid` 없이 결정론인가?
   - manifest에 빌드 시각이 없고 `content_hash`가 행 내용 기준인가(FP-033: 멱등성 키 = 출력을 결정하는 키)?
   - LLM·네트워크 호출이 없는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- LLM으로 행이나 자유 서술을 만들지 마라. 이유: 비용·일관성, M4 SQL 정답을 결정론적으로 계산해야 한다(ADR-024).
- SQLite 파일을 git에 추가하지 마라. 이유: 바이너리이고 시드에서 재생성된다. 재현성은 manifest의 `content_hash`로 확인한다.
- SQLite 파일 바이트 해시를 멱등성·재현성 키로 쓰지 마라. 이유: 같은 내용도 페이지 배치에 따라 바이트가 달라질 수 있다. 행 내용 해시를 쓴다(FP-033).
- 권한 열을 빼거나 "공개 범위"를 팀 목록만으로 표현하지 마라. 이유: M5·M6 `run_sql`의 권한 pre-filter(ADR-010)가 `access_level`·`access_teams`·`access_min_grade`에 의존한다.
- 행 수·권한 분포를 근사로 맞추지 마라(예: 비율 샘플링). 정확한 수를 테스트로 센다. 이유: FP-043, 보안 테스트셋이 제한 행 수에 의존한다.
- `src/kodocs/retrieval/`에 코드를 추가하지 마라. 이유: `run_sql`은 M5 범위다. 이 step은 DB 생성만 한다.
- step 0·1의 시드 스키마나 시드 내용을 고치지 마라. 시드가 규칙과 모순이면 `blocked`로 보고한다. 이유: 한 step 한 모듈, 시드는 사람 검토 대상.
- 기존 테스트를 깨뜨리지 마라.
