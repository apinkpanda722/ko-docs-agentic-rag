# Step 1: seed-data

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 합성 문서는 가상 회사 이름만, 실존 기관·인물 이름으로 기록을 만들지 않는다
- `docs/PRD.md` — 코퍼스 절(8개 팀, 기간, 식별자)
- `docs/ADR.md` — ADR-002, ADR-003, ADR-010, ADR-024
- `docs/EVAL.md` — 골드셋 유형(식별자·SQL 집계·다중 문서·권한·답 없음)
- step 0 산출물: `src/kodocs/synth/seed.py`(고정 계약·레이아웃·`validate_seed`), `src/kodocs/synth/identifiers.py`, `src/kodocs/synth/regulations.py`, `data/manifest/regulation_refs.json`, `tests/synth/`

## 배경

가상 IT 서비스 회사의 시드 데이터를 `data/seed/`에 채운다. 문서(step 5)와 SQLite(step 2)는 모두 이 시드에서 나온다. 시드는 **사람이 검토할 수 있게 작은 파일**로 나눈다. 인물은 수가 많으므로 결정론적 부트스트랩 스크립트로 만들고, 서사(장애·이벤트·서비스·규정 참조)는 이 step에서 직접 작성한다. LLM API를 쓰지 않는다.

## 작업

TDD: `tests/synth/test_seed_committed.py`(커밋된 시드 검사)를 먼저 작성하고, 시드를 채워 통과시킨다.

### 1. 인물 부트스트랩 — `src/kodocs/synth/bootstrap_people.py`

```python
def generate_people(*, random_seed: int, team_sizes: Mapping[str, int]) -> dict[str, list[dict]]: ...  # team → 인물 레코드
def main(argv=None) -> int: ...   # python -m kodocs.synth.bootstrap_people --out data/seed/people
```
- 가상 이름: 성·이름 음절 풀(코드 상수)에서 `random.Random(random_seed)`로 조합. 이름은 전 회사에서 유일.
- 팀당 팀장(grade 5) 정확히 1명. 임원(grade 6)은 `platform`·`finance`에 1명씩 총 2명. 나머지는 1~4등급을 분포(재량, 예: 사원 30%·선임 30%·책임 25%·수석 15%)로.
- `person_id`는 `p-001`부터 팀 코드 순·등급 내림차순으로 부여(결정론). `joined`는 2012-01-01~2026-05-31, `role`은 팀별 직무명 풀.
- 이미 파일이 있으면 덮어쓰지 않고 종료 코드 1(`--force`로만 덮어씀). 이유: 사람이 검토·수정한 시드를 스크립트가 조용히 덮어쓰면 안 된다.
- 테스트: 같은 시드 → 같은 출력, 다른 시드 → 다른 출력, 팀장 1명/팀, 이름 유일.
- 이 step에서 한 번 실행해 `data/seed/people/{team}.yaml`을 만든다.

### 2. 시드 파일 작성 (`data/seed/`, step 0 레이아웃 그대로)

**수량 기준(FP-043: 무엇을 세는지 명시)** — 커밋 시드 테스트가 정확히 센다:

| 대상 | 기준 | 세는 방법 |
|---|---|---|
| 인물 | 정확히 200명 | `people/*.yaml`의 레코드 수. 팀별: platform 42, payments 34, data-ai 30, sre 26, security 18, hr 18, finance 20, legal 12 |
| 팀 | 정확히 8개 | `teams.yaml` |
| 서비스 | 12개 이상 | `services.yaml`. `billing-api` 필수. 모든 서비스에 소유 팀. 서비스당 사실 4개 이상(소유 팀, 의존 서비스, SLO, 온콜 담당자 등) |
| 장애 | 정확히 25건 | `incidents/*.yaml` 파일 수. ID `INC-2025-NNN` 10건 + `INC-2026-NNN` 15건(연도별 번호 001부터 증가, 빈 번호 허용). `INC-2026-031` 포함 |
| 장애 사실 | 장애당 6개 이상 | 발생 시각(datetime), 복구 시각(datetime), 영향 서비스(service), 심각도(text), 원인(text), 조치(text), 담당자(person), 관련 티켓(id) 중 |
| 이벤트 | 60건 이상 | `events/*.yaml`의 레코드 수. 유형 태그 `project`·`decision`·`release`·`policy`·`org`·`sensitive`. 이벤트당 사실 3개 이상 |
| 제한 사실 | 30개 이상 | `sensitivity: restricted`인 사실 수. `sensitive` 이벤트(인사 평가 조정, 보안 취약점 미공개 조치, 법무 분쟁 대응, 재무 인수 검토 등)에 둔다. 8개 팀 중 6개 팀 이상에 분산 |
| 앵커 티켓 | 60건 이상 | `ledger/tickets_anchor.yaml`. 장애마다 1건 이상 연결(`incident_id`). `PAY-1234` 포함 |
| 앵커 결재 | 30건 이상 | `ledger/approvals_anchor.yaml`. `EXP-2026-0412` 포함. 각 결재에 `regulation_ref`(예: `출장규칙 제8조`) |
| 규정 참조 | 30항목 이상, 규정 10개 이상 | `regulation_refs.yaml`. 항목 = (규정명, 조 키, 참조 맥락 한 줄, 관련 팀) |
| 금지 이름 | 7개 이상 | `banned_names.yaml`: 한국지능정보사회진흥원, 지능정보사회진흥원, 한국정보화진흥원, 정보화진흥원, 한국정보사회진흥원, 한국전산원, NIA |

- `company.yaml`: 가상 회사명은 **`해온클라우드`**(영문 `Haeon Cloud`)로 고정한다. `random_seed: 20250101`, 기간, 인원 목표, 사업 개요(결제·데이터 플랫폼을 운영하는 B2B SaaS) 몇 줄.
- 모든 날짜는 2025-01-01~2026-06-30 안. 장애·이벤트는 기간 전체에 퍼뜨린다(분기마다 장애 2건 이상).
- 사실 사이의 **교차 연결**을 의도적으로 둔다: 같은 서비스에 장애 2건 이상인 서비스 3개 이상, 장애 원인이 이후 `decision`·`project` 이벤트로 이어지는 사례 5개 이상(이벤트의 사실에 장애 ID를 `id` 사실로 둔다). 이유: M4 다중 문서 문항의 재료다.
- 규정 참조(`regulation_refs.yaml`·`regulation` 사실·결재의 `regulation_ref`)는 `data/manifest/regulation_refs.json`에 있는 규정명·조 키만 쓴다. 조 제목을 보고 맥락이 맞는 조를 고른다(예: 출장비 결재 → `출장규칙 제8조`(여비 및 여행일수의 계산)). 맥락 문장에 **규정 내용을 단정적으로 옮겨 적지 않는다**("출장비 정산 시 출장규칙 제8조를 참조" 수준). 이유: 틀린 인용 방지(ADR-002).
- `ledger/rules.yaml`: step 2가 해석할 배경 행 생성 규칙. **권한 분포를 여기 명시**한다(행 수 기준, 앵커 포함 전체):
  - tickets 전체 500행: `public` 300 · `team` 175 · `restricted` 25
  - approvals 전체 150행: `public` 30 · `team` 90 · `restricted` 30
  - restricted 행의 `min_grade`는 4, `teams`는 소유 팀 1개
  - 그 밖에 팀별 티켓 비중, 상태·우선순위·유형 분포, 결재 카테고리별 금액 범위(원), 상태 분포, 월별 볼륨 가중치(재량. 분포는 합이 1이 되게)
  - 앵커 행에도 `access_level`을 명시한다(앵커의 제한 행은 위 25·30에 포함)
- `README.md`: 파일별 역할, 수정 시 `make verify`가 시드를 검증한다는 안내, 부트스트랩 재실행은 `--force`가 필요하다는 안내.

### 3. 커밋 시드 테스트 — `tests/synth/test_seed_committed.py`

- `load_seed(repo_root)` → `validate_seed(seed, regulation_index=load_index(repo_root))`가 빈 목록.
- 위 표의 수량을 표에 적힌 방법대로 정확히 센다(“이상”은 하한, “정확히”는 등호).
- 식별자 예시(`PAY-1234`, `EXP-2026-0412`, `INC-2026-031`, `billing-api`, `출장규칙 제8조`)가 시드에 존재.
- 시드 전체 텍스트에서 `find_banned_names` 결과 0건.
- `rules.yaml`의 권한 분포 합이 tickets 500·approvals 150과 같고, 앵커의 제한 행 수가 분포 안에 들어간다.

## Acceptance Criteria

```bash
make verify
uv run pytest -q tests/synth/test_seed_committed.py
ls data/seed/incidents | wc -l    # 25
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 수량 기준을 표의 "세는 방법"대로 테스트하는가(FP-043)?
   - 규정 참조가 전부 색인에 있는가? 규정 내용을 단정적으로 옮겨 적은 문장이 없는가?
   - 실존 기관·실존 기업·실존 인물 이름이 없는가? 회사명은 `해온클라우드`만 쓰는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 실존 기관·기업·인물 이름을 시드에 쓰지 마라(외부 서비스명 AWS·Slack 같은 일반 도구명은 허용). 이유: AGENTS.md CRITICAL, ADR-002 — 실존 이름으로 회의록·티켓 같은 기록을 만들면 허위 기록이 된다.
- 규정 조문 내용을 시드에 옮겨 적지 마라. 규정명·조 번호·짧은 참조 맥락만 쓴다. 이유: 원문 재배포 금지, 틀린 인용 방지(ADR-002).
- `data/manifest/regulation_refs.json`에 없는 규정·조를 참조하지 마라. 이유: 존재하지 않는 조항 인용은 검증에서 전부 실패한다.
- LLM API를 호출하거나 네트워크 요청을 하지 마라. 이유: 시드는 결정론·무비용이어야 하고, 실제 API 호출은 사용자가 한다(ADR-014, FP-040).
- step 0의 스키마·정규식·레이아웃을 바꾸지 마라. 필요한 필드가 있으면 엔티티 레코드의 추가 키로 둔다. 이유: 한 step 한 모듈, 계약 고정.
- 부트스트랩이 기존 시드 파일을 `--force` 없이 덮어쓰게 만들지 마라. 이유: 사람이 검토·수정한 시드 보존.
- 기존 테스트를 깨뜨리지 마라.
