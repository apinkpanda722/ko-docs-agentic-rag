# Step 0: seed-schema

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — CRITICAL 규칙(합성 문서는 가상 회사 이름만, 실제 규정은 기관명 없이 규정명·조항만, `data/interim/` 비커밋)
- `docs/ADR.md` — ADR-002, ADR-003(보완 포함), ADR-010, ADR-017, **ADR-024**
- `docs/ARCHITECTURE.md` — `data/seed/`, `data/synthetic/`, 상태 관리의 `access` 형식
- `docs/EVAL.md` — 합성 qrels는 시드 사실 ID·문자 범위
- `src/kodocs/parsing/articles.py` — `law_to_lines()`, `split_text()`, `Article`(number·branch·title·deleted). 조 키 추출은 이 분할기를 재사용한다
- `src/kodocs/ingest/manifest.py` — `now_iso()`, `sha256_file()`
- `tests/parsing/test_articles.py` — 분할기 테스트 방식 참고

## 배경

M3는 가상 IT 서비스 회사의 합성 코퍼스를 만든다. 이 step은 **이후 모든 step이 공유하는 계약**(식별자 형식, 팀·직급 코드, 권한 형식, 사실(fact) 스키마, 시드 로더·검증기, 규정 색인)을 만든다. 시드 데이터 내용은 step 1에서 채운다. 여기서는 스키마·검증기와 fixture 기반 테스트, 규정 색인만 만든다.

### 고정 계약 (이후 step이 그대로 쓴다. 바꾸지 마라)

- 기간: `PERIOD_START = date(2025, 1, 1)`, `PERIOD_END = date(2026, 6, 30)` (양 끝 포함)
- 팀 코드 8개(코드 → 한글명 → 티켓 프로젝트 키):
  `platform` 플랫폼개발 `PLT` · `payments` 결제서비스 `PAY` · `data-ai` 데이터AI `DAI` · `sre` SRE인프라 `SRE` ·
  `security` 정보보안 `SEC` · `hr` 인사 `HRM` · `finance` 재무 `FIN` · `legal` 법무컴플라이언스 `LGL`
- 직급(정수 등급): `1` 사원, `2` 선임, `3` 책임, `4` 수석, `5` 팀장, `6` 임원
- 권한: `Access(level: Literal["public","team","restricted"], teams: tuple[str, ...], min_grade: int)`
  - `public`: `teams=()`, `min_grade=1`
  - `team`: `teams`에 1개 이상 팀 코드, `min_grade=1`
  - `restricted`: `teams`에 1개 이상 팀 코드, `min_grade >= 4`
- 식별자 정규식(`src/kodocs/synth/identifiers.py`, 모듈 상수로 컴파일):
  - 티켓 `TICKET_RE = r"(?<![A-Z0-9-])(PLT|PAY|DAI|SRE|SEC|HRM|FIN|LGL)-\d{4}(?![\d-])"` (예: `PAY-1234`)
  - 결재 `APPROVAL_RE = r"(?<![A-Z0-9-])EXP-(2025|2026)-\d{4}(?![\d-])"` (예: `EXP-2026-0412`)
  - 장애 `INCIDENT_RE = r"(?<![A-Z0-9-])INC-(2025|2026)-\d{3}(?![\d-])"` (예: `INC-2026-031`)
  - 서비스 `SERVICE_RE`: 시드에 등록된 서비스명 목록에서 만드는 함수 `service_pattern(names) -> re.Pattern`(kebab-case, 앞뒤가 `[a-z0-9-]`가 아닐 때만 일치)
  - 규정 조항 `REGULATION_REF_RE`: `(?P<name>[가-힣A-Za-z0-9·\s]+?(규칙|규정|지침|기준|헌장))\s*제(?P<num>\d+)조(의(?P<branch>\d+))?` — 이름 앞 경계 처리는 재량. 테스트로 `출장규칙 제8조`, `인사규정 제12조의2`, `정보보안 기본지침 제3조`를 인식하는지 확인한다
  - `extract_identifiers(text) -> dict[str, list[tuple[str, int, int]]]` (종류별 (값, start, end))
  - `EXP-2026-0412`의 `EXP-2026` 부분이나 `INC-2026-031`의 `INC-2026`이 티켓으로 잡히지 않아야 한다(테스트 필수).
- fact_id 형식: `^f-[a-z0-9]+(-[a-z0-9]+)*$` (예: `f-inc-2026-031-cause`). 소문자만. 전 시드에서 유일.
- 사실 종류 `FactKind = Literal["id","date","datetime","amount","person","service","count","text","regulation"]`
  - 원자 사실(`id`·`date`·`datetime`·`amount`·`person`·`service`·`count`·`regulation`)은 `value`가 정규 형식이다:
    `id`는 식별자 문자열, `date`는 `YYYY-MM-DD`, `datetime`은 `YYYY-MM-DDTHH:MM`(KST), `amount`는 원 단위 정수, `person`은 인물 ID(`p-001` 형식), `service`는 서비스명, `count`는 정수, `regulation`은 `"출장규칙 제8조"` 형식
  - `text`는 서술형 사실(원인·결정·조치). `value`는 한 문장 요지다(본문에 그대로 나올 필요 없음)
- 사실 레코드: `Fact(fact_id, kind, value, subject, date: date | None, sensitivity: Literal["normal","restricted"], source: str)`
  - `subject`: 이 사실이 속한 엔티티 ID(예: `INC-2026-031`, `billing-api`, `p-017`, `ev-2025-014`)
  - `date`: 사실이 성립한 날짜(없으면 None). 기간 안이어야 한다
  - `sensitivity="restricted"`인 사실은 step 3에서 제한 문서에만 배정된다
  - `source`: 사실이 정의된 시드 파일의 저장소 상대 경로
- 원자 사실의 **허용 표기(surface forms)**: `surface_forms(fact, *, people) -> tuple[str, ...]`
  - `date` `2026-03-14` → `2026-03-14`, `2026.3.14`, `2026. 3. 14.`, `2026년 3월 14일`, `3월 14일`
  - `datetime` → 날짜 표기 + ` 14:05` / ` 14시 5분` 조합
  - `amount` `1250000` → `1,250,000원`, `1250000원`, `125만 원`, `125만원`, `1,250,000 원`(만 단위로 나누어떨어질 때만 만 표기)
  - `person` → 인물의 이름(예: `김서연`)과 `이름 + 직급명`(예: `김서연 책임`)
  - `count` → `12`, `12건`, `12개`, `12명`, `12회`
  - `id`·`service`·`regulation` → 값 그대로
  - step 6 검증기가 "표시 구간 안에 허용 표기 중 하나가 있는가"로 쓴다
- 금지 이름: `data/seed/banned_names.yaml`(step 1에서 작성)을 읽는 `load_banned_names(root)`와, 본문에서 찾는 `find_banned_names(text, names) -> list[tuple[str,int,int]]`. ASCII 이름(예: `NIA`)은 앞뒤가 영문자·숫자가 아닐 때만 일치한다.

## 작업

TDD: 테스트를 먼저 작성한다. 시드 fixture는 `tests/synth/fixtures/seed_min/`에 최소 크기(팀 2·인물 4·서비스 1·장애 1 정도)로 만든다.

### 1. 의존성

- `pyyaml`을 **메인 의존성**(`[project].dependencies`)으로 옮긴다(`uv add pyyaml`, dev 그룹에서 제거). `uv.lock`을 갱신한다.

### 2. `src/kodocs/synth/identifiers.py`

위 계약의 정규식과 `extract_identifiers`, `service_pattern`. 이 모듈은 `yaml`·`kodocs.synth.seed`를 import하지 않는다(M5 적재에서도 쓰기 때문).

### 3. `src/kodocs/synth/seed.py`

```python
PERIOD_START: date; PERIOD_END: date
TEAMS: Mapping[str, TeamCode]          # code → (name_ko, project_key)
GRADES: Mapping[int, str]
@dataclass(frozen=True) class Access: ...
@dataclass(frozen=True) class Person: person_id, name, team, grade, role, joined: date
@dataclass(frozen=True) class Service: name, owner_team, description, facts: tuple[Fact, ...]
@dataclass(frozen=True) class Fact: ...
@dataclass(frozen=True) class Seed:
    company: Mapping[str, object]        # 가상 회사명·random_seed 등
    people: tuple[Person, ...]
    services: tuple[Service, ...]
    facts: Mapping[str, Fact]            # fact_id → Fact (모든 파일의 사실 합집합)
    entities: Mapping[str, Mapping[str, object]]  # subject ID → 원본 레코드(장애·이벤트·앵커 티켓·결재)
    regulation_refs: tuple[Mapping[str, object], ...]
    banned_names: tuple[str, ...]
    ledger_rules: Mapping[str, object]   # step 2가 쓰는 티켓·결재 생성 규칙 원본
class SeedError(ValueError): ...         # 모든 위반을 모아 한 번에 보고(첫 위반에서 멈추지 않음)
def load_seed(root: Path) -> Seed: ...   # root/data/seed/ 아래 파일을 읽는다. 파일 순서는 정렬해 결정론
def validate_seed(seed: Seed, *, regulation_index: Mapping[str, object]) -> list[str]: ...  # 위반 메시지 목록(빈 목록이면 통과)
def surface_forms(fact: Fact, *, people: Mapping[str, Person]) -> tuple[str, ...]: ...
def load_banned_names(root: Path) -> tuple[str, ...]: ...
def find_banned_names(text: str, names: Sequence[str]) -> list[tuple[str, int, int]]: ...
```

시드 디렉토리 레이아웃(step 1이 이 레이아웃으로 채운다. 로더는 이 레이아웃을 전제로 한다):

```
data/seed/
├── README.md               # 사람 검토 안내 (step 1)
├── company.yaml            # 가상 회사명, random_seed, 기간
├── teams.yaml              # 팀 코드·한글명·프로젝트 키·인원 목표
├── people/{team}.yaml      # 팀별 인물 목록
├── services.yaml           # 서비스와 서비스 사실
├── incidents/*.yaml        # 장애 1건당 파일 1개 (INC-YYYY-NNN.yaml)
├── events/*.yaml           # 프로젝트·결정·릴리스·공지 이벤트(여러 건/파일 가능)
├── ledger/tickets_anchor.yaml     # 문서가 언급하는 앵커 티켓
├── ledger/approvals_anchor.yaml   # 문서가 언급하는 앵커 결재
├── ledger/rules.yaml              # 배경 행 생성 규칙·권한 분포 (step 2가 해석)
├── regulation_refs.yaml    # 합성 문서가 참조할 규정 조항과 참조 맥락
└── banned_names.yaml       # 실존 기관명 금지 목록
```

엔티티 파일의 공통 형식(각 레코드): `id`, `title`, `date`(또는 `start`), `owner_team`, `sensitivity`(기본 normal), `facts: [{fact_id, kind, value, date?}]`. 로더는 각 fact의 `subject`를 레코드 `id`로, `source`를 파일 경로로 채운다.

`validate_seed` 검사 항목(각각 fixture 위반 사례 테스트 필수):
1. fact_id 형식·전역 유일, person_id(`p-\d{3}`) 유일, 엔티티 ID 유일
2. 식별자 형식: 장애 ID는 `INCIDENT_RE`, 앵커 티켓은 `TICKET_RE`이고 프로젝트 키가 소유 팀과 일치, 결재는 `APPROVAL_RE`이고 연도가 제출일 연도와 일치
3. 날짜가 기간 안(사실 `date`, 엔티티 날짜, 인물 입사일은 기간 이전 허용)
4. 참조 무결성: `person` 사실의 값·엔티티의 담당자가 존재하는 인물, `service` 값이 등록된 서비스, 팀 코드가 8개 중 하나, 앵커 티켓의 `incident_id`가 존재하는 장애
5. `kind`별 값 형식(날짜 파싱, 금액·건수 양의 정수, `regulation` 값이 `REGULATION_REF_RE`와 일치)
6. `regulation` 사실과 `regulation_refs.yaml`의 모든 항목이 `regulation_index`에 있는 규정명·조 키일 것(삭제된 조 제외)
7. 금지 이름이 시드의 어떤 문자열 값에도 없을 것(재귀 검사)
8. 인원 수·팀 수 같은 **목표 수량은 여기서 검사하지 않는다**(fixture가 작기 때문). 수량 검사는 step 1 테스트가 커밋된 시드에 대해 한다.

### 4. `src/kodocs/synth/regulations.py` — 규정 색인

```python
INDEX_PATH = Path("data/manifest/regulation_refs.json")
def build_index(law_dir: Path, *, banned_names: Sequence[str]) -> dict[str, object]: ...
def load_index(root: Path) -> dict[str, object]: ...
def main(argv: Sequence[str] | None = None) -> int: ...   # python -m kodocs.synth.regulations
```

- 입력: `data/interim/law/*/*.json` 중 `is_current == true`인 버전(규정마다 1개).
- 조 키는 `law_to_lines()` → `split_text()`의 `Article`로 만든다: 키 `제{number}조` 또는 `제{number}조의{branch}`, `deleted`는 제외.
- 출력 형식(결정론, `json.dumps(..., ensure_ascii=False, sort_keys=True, indent=2)`):
  `{"generated_from": "data/interim/law (현행)", "rule_count": N, "rules": {"출장규칙": {"rule_id": "...", "serial": "...", "articles": {"제8조": "여비 및 여행일수의 계산", ...}}, ...}}`
  - 조 **제목만** 넣는다(없으면 `null`). **조문 본문은 절대 넣지 않는다**. 이유: `data/interim/`은 원문이라 커밋하지 않는다(AGENTS.md, ADR-002). 이 색인은 커밋한다.
  - 규정명은 `name_without_agency`를 쓴다. 규정명에 금지 이름(예: `NIA`)이 포함된 규정은 색인에서 빼고 `excluded: [{"rule_id", "reason"}]`에 이름 없이 사유만 남긴다(예: `"banned_name_in_title"`).
- `make reg-index` 타깃을 추가하고, **이 step 안에서 실제로 한 번 실행해 `data/manifest/regulation_refs.json`을 만든다**(로컬 파일만 읽음, 네트워크 없음). `data/interim/law`가 없으면 안내 후 종료 코드 2.
  - 이 시점에는 `data/seed/banned_names.yaml`이 없다. `build_index`의 기본 금지 이름은 `("한국지능정보사회진흥원", "지능정보사회진흥원", "한국정보화진흥원", "정보화진흥원", "한국정보사회진흥원", "한국전산원", "NIA")`로 두고, 파일이 있으면 파일을 쓴다.
- 현행 규정이 0개면 실패로 멈춘다(FP-031). 이유: 경로·필드가 조금만 달라도 빈 색인이 성공처럼 커밋된다.
- 테스트: fixture 법제처 JSON 2~3개(현행·연혁 섞음, 삭제 조 포함, 금지 이름 규정 포함)로 색인 내용, 조문 본문 문자열이 출력에 없음, 결정론(두 번 실행 동일), 0개 실패.
- 커밋된 색인에 대한 테스트: `data/manifest/regulation_refs.json`이 존재하고 `rules`에 `출장규칙`이 있으며 `제8조` 키가 있는지. 어떤 값도 80자를 넘지 않는지(본문 유입 방지).

### 5. Makefile·AGENTS.md

- `reg-index: uv run python -m kodocs.synth.regulations` (`.PHONY` 추가)
- AGENTS.md 명령어 절에 `make reg-index   # 법제처 현행 규정의 규정명·조 번호·조 제목 색인 → data/manifest/regulation_refs.json (조문 본문 없음, 네트워크 없음)` 추가

## Acceptance Criteria

```bash
make verify
make reg-index
python3 -c "import json;d=json.load(open('data/manifest/regulation_refs.json'));assert d['rule_count']>=90 and '제8조' in d['rules']['출장규칙']['articles'];print(d['rule_count'])"
grep -n "pyyaml" pyproject.toml
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - `EXP-2026-0412`·`INC-2026-031`이 티켓 정규식에 잡히지 않는 테스트가 있는가?
   - `validate_seed`가 위반을 모두 모아 보고하는가(위반 2개 fixture → 메시지 2개)?
   - 색인에 조문 본문이 없는가(테스트로 강제)?
   - `identifiers.py`가 `yaml`·`seed`를 import하지 않는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 색인·시드·테스트 fixture에 법제처 조문 본문을 넣지 마라. 이유: 원문 재배포 금지(ADR-002, AGENTS.md). 조 제목까지만 허용한다.
- 위 "고정 계약"의 형식(팀 코드·프로젝트 키·정규식·fact_id 형식·권한 형식·허용 표기)을 바꾸지 마라. 이유: step 1~8과 M4·M5가 이 계약에 의존한다.
- 네트워크 요청을 하지 마라(의존성 설치 제외). 이유: FP-040. 색인은 로컬 `data/interim/law`만 읽는다.
- 현행 규정 0개·조 0개를 성공으로 끝내지 마라. 이유: FP-031(필터 결과 0건이 성공으로 끝남).
- 목표 수량(인원 200 등)을 fixture 기반 스키마 테스트에 넣지 마라. 이유: 수량은 커밋된 시드 기준으로 step 1에서 센다(FP-043: 무엇을 세는지 분리).
- `src/kodocs/parsing/articles.py`를 수정하지 마라. 이유: M2 채점 결과가 이 분할기에 묶여 있다. 필요한 변환은 `regulations.py` 안에서 한다.
- `data/interim/`의 파일을 git에 추가하지 마라. 이유: AGENTS.md CRITICAL.
- 기존 테스트를 깨뜨리지 마라.
