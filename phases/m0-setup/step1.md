# Step 1: opensearch-compose

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 기술 스택, CRITICAL 규칙, 명령어
- `docs/ADR.md` — **ADR-005를 반드시 읽어라.** OpenSearch 버전·구성·플러그인·트레이드오프가 전부 여기에 있다.
  ADR-017(식별자 `keyword` 필드)도 인덱스 설계 배경으로 읽어라.
- `docs/ARCHITECTURE.md` — `infra/` 위치, 인덱스 상태 관리 방침
- `docs/PRD.md` — 마일스톤 표의 M0 행
- 이전 step(step 0)에서 만들어진 파일: `pyproject.toml`, `Makefile`, `tests/test_architecture.py`,
  `src/kodocs/` 패키지 골격

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라.

## 배경

step 0에서 uv 프로젝트 골격과 `make verify`(= `uv run ruff check .` + `uv run ruff format --check .` +
`uv run pytest -q`)가 확정되었다. 이 step은 검색 엔진을 로컬에서 띄우는 Docker 구성을 추가한다.

핵심 제약: **`make verify`는 Docker 없이, 네트워크 없이 통과해야 한다**(AGENTS.md CRITICAL).
따라서 이 step의 기본 테스트는 컨테이너를 띄우지 않고 구성 파일만 검증한다.
실제 기동 확인은 `integration` 마커를 붙여 기본 실행에서 제외하고, 사용자가 직접 돌린다.

## 작업

### 1. `infra/Dockerfile`

OpenSearch 공식 이미지에 한국어 형태소 분석기 플러그인을 설치한 이미지를 빌드한다.

- 베이스 이미지는 **`opensearchproject/opensearch:3.8.0`으로 핀한다.** `latest`나 `3`, `3.8` 같은
  움직이는 태그를 쓰지 마라. 이유: 모든 검색 수치가 재현 가능해야 하고, 엔진 버전이 바뀌면
  BM25·hybrid 결과가 조용히 달라진다.
- `analysis-nori` 플러그인을 이미지 빌드 시 설치한다(`opensearch-plugin install --batch analysis-nori`).
  nori는 기본 배포판에 번들되어 있지 않은 추가 플러그인이다. 반면 k-NN과 neural-search(hybrid,
  normalization·score-ranker processor)는 기본 번들이므로 따로 설치하지 마라.
- 빌드된 이미지 태그는 `kodocs-opensearch:3.8.0`으로 한다.

### 2. `infra/docker-compose.yml`

단일 노드 로컬 개발 구성이다.

- 서비스 이름 `opensearch`. 1번의 `Dockerfile`을 `build`로 참조하고 `image`에 `kodocs-opensearch:3.8.0`을 준다.
- 환경변수:
  - `discovery.type=single-node`
  - `DISABLE_SECURITY_PLUGIN=true` — 로컬 개발에서 보안 플러그인을 끈다(ADR-005). 권한은 앱 계층
    `retrieval.authorized_*`에서 강제한다(ADR-010).
  - `DISABLE_INSTALL_DEMO_CONFIG=true` — 보안 플러그인을 끌 때 demo 인증서·기본 관리자 계정 설치도
    함께 끈다. 이유: 끈 보안 설정의 데모 자격증명이 이미지에 남는 것을 막는다.
  - `OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g` — ADR-005의 메모리 요구(Docker Desktop 4GB 이상)에 맞춘다.
  - `bootstrap.memory_lock=true` + 대응하는 `ulimits.memlock`(soft/hard `-1`), `ulimits.nofile`
- 포트는 **`127.0.0.1:9200:9200`으로만 바인딩한다.** `9200:9200`이나 `0.0.0.0:9200:9200`을 쓰지 마라.
  이유: 보안 플러그인을 껐기 때문에 인증 없는 엔드포인트가 되고, 같은 네트워크의 다른 기기에
  노출되면 사내 문서 색인이 무인증으로 읽힌다.
- 데이터는 named volume에 저장한다(예: `opensearch-data`). bind mount를 쓰지 마라.
  이유: 인덱스는 재생성 가능한 산출물이고(ARCHITECTURE.md 상태 관리), 호스트 경로 권한 문제를 피한다.
- OpenSearch Dashboards 서비스는 넣지 마라. 이유: 이 프로젝트는 API로만 검색하고, 컨테이너가 늘면
  메모리 요구가 올라간다.

### 3. `Makefile` 타깃 추가

```makefile
up:               # docker compose -f infra/docker-compose.yml up -d --build
down:             # docker compose -f infra/docker-compose.yml down
logs:             # docker compose -f infra/docker-compose.yml logs -f opensearch
test-integration: # uv run pytest -q -m integration
```

- `.PHONY`에 추가한다.
- `verify` 타깃은 건드리지 마라. Docker를 띄우는 명령을 `verify`에 넣지 마라.

### 4. pytest `integration` 마커 등록

`pyproject.toml`의 `[tool.pytest.ini_options]`에:
- `markers`에 `integration` 마커를 등록한다(설명: 실행 중인 OpenSearch가 필요한 테스트).
- `addopts`로 기본 실행에서 제외한다: `-m "not integration"`.
- `make test-integration`이 `-m integration`을 넘겨 addopts를 덮어쓰고 통합 테스트만 돌아가는지
  실제로 확인하라(뒤에 온 `-m`이 이긴다). 안 되면 `make test-integration`에서
  `-m "integration"`을 명시적으로 넘기는 방식으로 맞춘다.

### 5. `tests/test_infra_compose.py` — 구성 파일 검증 (Docker 없이)

step 0에서 dev 의존성으로 넣어둔 `pyyaml`로 compose 파일을 파싱해 검증한다.
아래 항목은 전부 회귀 방지 대상이다. 각각 독립된 테스트 함수로 쓰고, 실패 메시지에 무엇이 어긋났는지 담는다.

- `infra/Dockerfile`의 `FROM`이 `opensearchproject/opensearch:3.8.0`이다(움직이는 태그가 아니다).
- `infra/Dockerfile`이 `analysis-nori`를 설치한다.
- compose의 `opensearch` 서비스 환경변수에 `DISABLE_SECURITY_PLUGIN=true`와
  `DISABLE_INSTALL_DEMO_CONFIG=true`가 있다.
- 포트 매핑이 `127.0.0.1`에 바인딩되어 있다. `0.0.0.0` 또는 호스트 IP 없는 `9200:9200` 형태면 실패한다.
- `discovery.type=single-node`가 설정되어 있다.
- 데이터 볼륨이 named volume이다(`./` 또는 `/`로 시작하는 bind mount면 실패한다).

환경변수는 compose에서 리스트(`KEY=value`)와 매핑(`KEY: value`) 두 형식 모두 가능하다.
어느 형식을 쓸지 정하고, 테스트는 그 형식을 읽으면 된다. 형식 두 개를 모두 지원하는
범용 파서를 만들지 마라(쓰이지 않는 분기다).

### 6. `tests/test_opensearch_integration.py` — 실제 기동 검증 (`integration` 마커)

모든 테스트에 `@pytest.mark.integration`을 붙인다. 검증 항목:

- 클러스터 health가 응답하고 status가 `green` 또는 `yellow`다(단일 노드라 복제본 미할당으로
  `yellow`가 정상이다. `green`만 통과시키면 안 된다).
- `_cat/plugins`에 `analysis-nori`가 있다.
- `_analyze`에 `nori_tokenizer`로 한국어 문장을 넣으면 형태소 단위로 쪼개진다.
  예: `"여비규정 제8조의 출장비 지급 기준"` → 토큰이 2개보다 많고 `여비` 같은 형태소가 나온다.
  기대 토큰 목록을 통째로 하드코딩하지 마라. 이유: 사전 버전에 따라 분해 결과가 달라져 깨진다.
  "형태소 단위로 쪼개진다"는 성질만 확인한다.

HTTP 호출은 **표준 라이브러리 `urllib.request`로 한다.** `opensearch-py`나 `requests`를
의존성에 추가하지 마라. 이유: M0은 클라이언트 라이브러리를 고르는 단계가 아니고(M5 범위),
통합 테스트 하나 때문에 runtime 의존성이 늘면 안 된다.

엔드포인트는 `http://127.0.0.1:9200`을 기본값으로 쓰되, 환경변수(예: `OPENSEARCH_URL`)로
덮어쓸 수 있게 한다. 연결이 안 되면 무엇을 실행해야 하는지(`make up`) 알려주는 메시지로 실패한다.

### 7. `infra/README.md`

- 고른 이미지 태그(`opensearchproject/opensearch:3.8.0`)와 선택 이유를 적는다: Docker Hub의 3.x 최신
  안정 버전이고 `analysis-nori` 3.8.0 아티팩트가 존재한다. 그리고 ADR-005의 근거 — hybrid 쿼리의
  공통 filter가 k-NN 하위 쿼리에 효율적 사전 필터로 적용되는 것이 3.0부터다.
- 메모리 요구사항(Docker Desktop 4GB 이상, 힙 2g)과 `vm.max_map_count` 관련 주의를 적는다.
- 실행 순서: `make up` → 기동 대기 → `make test-integration` → `make down`.
- 보안 플러그인을 끈 구성이고 `127.0.0.1`에만 바인딩한다는 점, 이것이 로컬 개발 전용이라는 점을 명시한다.

## Acceptance Criteria

```bash
make verify                              # Docker 없이 통과해야 한다 (integration 테스트는 제외됨)
uv run pytest -q -m integration --collect-only   # 통합 테스트가 수집은 되는지 확인 (실행 아님)
uv run pytest -q tests/test_infra_compose.py     # 구성 검증 테스트가 통과한다
docker compose -f infra/docker-compose.yml config   # compose 파일이 문법적으로 유효하다
```

`docker compose config`는 이미지를 내려받거나 컨테이너를 띄우지 않는다. Docker가 없는 환경이라
이 명령이 실패하면 `"status": "blocked"`가 아니라, 나머지 AC를 통과시킨 뒤 summary에 그 사실을 적어라.

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - `docs/ARCHITECTURE.md`의 `infra/` 위치를 따르는가?
   - ADR-005의 구성(3.x, nori는 플러그인, k-NN·neural-search는 번들, 보안 플러그인 off)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙을 위반하지 않았는가? (`make verify`가 Docker·네트워크·API 키 없이 통과)
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 테스트나 `make verify` 안에서 `docker compose up`, `docker build`, `docker run`을 실행하지 마라.
  이유: `make verify`는 Docker 없는 환경에서도 통과해야 하고, `scripts/execute.py`가 step마다 이
  명령을 직접 다시 돌린다. 컨테이너 기동은 분 단위로 걸리고 실패 원인을 코드 문제와 구분할 수 없다.
- 포트를 `0.0.0.0`이나 호스트 IP 없이 바인딩하지 마라. 이유: 보안 플러그인을 껐으므로 무인증
  엔드포인트가 되고 같은 네트워크에서 색인을 읽을 수 있다.
- `latest`·`3`·`3.8` 같은 움직이는 태그를 쓰지 마라. 이유: 엔진 버전이 바뀌면 검색 수치가 조용히 달라진다.
- `opensearch-py`, `requests`, `httpx`를 의존성에 추가하지 마라. 이유: 검색 클라이언트 선택은 M5 범위다.
  통합 테스트는 stdlib `urllib.request`로 충분하다.
- 인덱스 매핑·분석기 설정·템플릿·적재 코드를 만들지 마라. 이유: 분석기 변형(standard/nori/n-gram)과
  `keyword` 식별자 필드는 M5의 측정 대상이고, 여기서 미리 정하면 실험 설계를 앞질러 고정한다.
- `src/kodocs/retrieval/`에 코드를 추가하지 마라. 이유: 이 step의 범위는 인프라 구성이다.
- OpenSearch Dashboards, 보안 플러그인 설정, TLS 인증서를 추가하지 마라. 이유: 로컬 데모 범위 밖이다.
- `Makefile`의 `verify` 타깃을 수정하지 마라. `scripts/` 아래 파일을 수정하지 마라.
- `.env` 파일을 열거나 출력하지 마라.
- 기존 테스트를 깨뜨리지 마라. 특히 `tests/test_architecture.py`와 `scripts/test_execute.py`가
  `make verify`에서 계속 수집·통과해야 한다.
