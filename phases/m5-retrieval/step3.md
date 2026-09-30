# Step 3: corpus-loader

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙 (retrieval의 langchain/langgraph 금지, 모든 검색은 authorized_*, LLM·외부 API mock, golden·raw·interim 비커밋)
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·상태 관리
- `docs/ADR.md` — ADR-005·006·007·009·010·013·015·016·017·026·028(M5 설계, step 0에서 추가됨)
- `docs/EVAL.md` — 검색 지표·결과 보고 절
- `docs/results/hypotheses.md` — H1~H7과 "M5 판정 규칙" 절(step 0에서 추가됨)
- `src/kodocs/parsing/annex_mapping.py`, `data/manifest/m5_annex_mapping.json` — step 2 산출물
- `src/kodocs/parsing/articles.py` — `split_document`, `law_to_lines`, `Article`
- `src/kodocs/synth/identifiers.py` — `extract_identifiers`, `service_pattern`
- `src/kodocs/ingest/manifest.py` — `latest_records`
- `data/synthetic/docs/*.json`(합성 문서 200건: 필드 `doc_id`·`doc_type`·`body`·`access`·`team`·`date`·`title`), `data/synthetic/docs/index.json`
- `data/manifest/parse_targets.json`, `data/manifest/version_matches.jsonl`, `data/interim/law/<rule_id>/<serial>.json`(법제처 연혁: `articles`·`issued_date`·`rule_id`·`serial`·`name`)
- `data/golden/queries/gold.jsonl` — temporal:revision 5문항의 정답 doc_id

이전 step에서 만들어진 코드를 꼼꼼히 읽고, 설계 의도를 이해한 뒤 작업하라. 이 phase의 결정(ADR-028)과 판정 규칙(hypotheses.md의 "M5 판정 규칙")은 사전 등록된 값이므로 바꾸지 마라.

## 작업

코퍼스 로더를 만든다. 신규 파일: `src/kodocs/chunking/corpus.py`, `tests/chunking/test_corpus.py`(`tests/chunking/__init__.py`가 필요하면 만든다).

### 시그니처

```python
ParserCondition = Literal["composite", "pypdf", "upstage"]

@dataclass(frozen=True)
class Access:
    level: str                    # public | team | restricted
    teams: tuple[str, ...]
    min_grade: int

@dataclass(frozen=True)
class Section:                    # 구조 청킹이 쓸 원문 구간
    kind: str                     # article | annex | addendum | heading_section | agenda | preamble
    key: str | None               # 조 키("제11조", "제5조의2") 또는 정규화 별표 id("별표2")
    path: tuple[str, ...]         # 경로 prefix(규정명 > 장 > 조 제목 등)
    start: int                    # doc.text 안의 문자 범위(반열림)
    end: int

@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str                   # 합성: syn-*, 실제: law-<serial>
    source: str                   # synthetic | real
    doc_type: str                 # meeting|weekly|runbook|guide|notice|incident|regulation
    title: str
    text: str                     # 청킹·qrels 문자 범위의 기준 텍스트
    sections: tuple[Section, ...]
    access: Access
    team: str | None
    date: str | None
    rule_id: str | None           # 실제 규정만
    is_current: bool
    superseded_by: str | None     # 개정 전 문서만: 현행 doc_id
    identifiers: dict[str, tuple[str, ...]]   # ticket|approval|incident|service|regulation

def load_corpus(condition: ParserCondition = "composite", *, root: Path = Path(".")) -> tuple[list[CorpusDoc], LoadReport]: ...
```

### 핵심 규칙

1. **합성 문서**: `text`는 `body`를 **한 글자도 바꾸지 않고** 쓴다. 이유: 골드·실버 qrels의 `start`/`end`가 이 body 기준이며 `span_sha256`으로 검증된다. 구조 구간은 마크다운 제목으로 나눈다: 회의록은 `### N.M` 안건 소절(상위 `## 3. 안건별 논의` 아래), 장애보고서는 `## N.` 섹션, 주간보고·런북·가이드·공지는 `##` 섹션. 문서 유형별로 실제 문서 몇 건을 읽어 제목 패턴을 확인한 뒤 정한다(FP-016: 200건 전체에 돌려 제목 구간이 0개로 나오는 문서 수를 유형별로 세고 `LoadReport`에 기록. 0개인 문서는 문서 전체를 구간 1개로 둔다).
2. **실제 규정 3조건**(`ParserCondition`)
   - `composite`(본 인덱스): 조문 텍스트 = `article_region_text(pypdf)`(별표 영역 제외), 그 뒤에 각 별표를 순서대로 붙인다. 별표 텍스트 = step 2 매핑 성공이면 업스테이지 표 텍스트(`AnnexTable.text`), `fallback`이면 pypdf 별표 영역 텍스트. `sections`에 조(article)·부칙·별표(annex) 구간의 문자 범위를 기록한다. 조 경로 prefix는 (규정명, 장 제목이 있으면 장, `제N조(제목)`).
   - `pypdf`: pypdf 텍스트 전체(별표 영역 포함, pypdf 그대로). 조·별표 구간은 pypdf 분할·`annex_regions`로 만든다.
   - `upstage`: 업스테이지 텍스트 전체. 조 구간은 업스테이지 텍스트에 같은 분할기를 적용해 만든다. 표는 업스테이지 표 텍스트를 별표 id에 붙인다.
   - 대상: `parse_targets.json`에서 `role == "current"`인 규정(95개 목표). 입력이 없는 대상은 예외로 멈추지 말고 `LoadReport.skipped`에 (규정, 사유)로 남기되, **로드된 실제 문서 수가 기대 수와 다르면 `LoadReport`에 명시**하고 CLI 요약이 눈에 띄게 출력한다.
   - `doc_id = f"law-{law_serial}"`(골드 qrels의 실제 문항 doc_id와 같은 규칙 — `data/golden/queries/gold.jsonl` 실제 문항으로 확인). `access = Access("public", (), 0)`.
3. **개정 전 5건**: temporal:revision 5문항의 정답 doc_id마다, `data/interim/law/<rule_id>/`의 버전들 중 발령일(`issued_date`)이 정답 문서 바로 앞인 버전을 찾아 `CorpusDoc`으로 만든다(`is_current=False`, `superseded_by=현행 doc_id`, `text`는 `law_to_lines` 기반 법제처 조문 텍스트, 조 구간은 같은 분할기로). 조합 조건과 무관하게 3조건 모두 동일하다. 5건의 (현행 serial, 직전 serial, 두 발령일)을 `data/manifest/m5_prior_versions.json`에 기록하는 함수 `write_prior_versions_manifest`도 만든다(원문 텍스트 금지). 직전 버전이 없으면 예외 대신 목록에 남기고 CLI가 실패로 종료한다(5건 모두 있어야 한다).
4. **식별자**: `extract_identifiers`로 ticket·approval·incident를, 서비스명은 `service_pattern`으로(서비스 목록은 `data/synthetic/ledger.sqlite`의 `services` 테이블 또는 시드 YAML에서 읽는다 — 어느 쪽을 쓰는지 `LoadReport`에 기록), 규정 조항 참조는 `identifiers["regulation"]`에 `"{규정명} 제N조"`를 공백 제거·NFC 정규화한 값으로 넣는다. 이 정규화 함수(`normalize_regulation_ref`)는 하나만 만들어 질의 쪽에서도 재사용한다(step 9). 실제 규정 문서는 각 조에 대해 `"{규정명} 제N조"` 값을 식별자로 갖는다(문서 단위 목록).
5. **검증 테스트**: (a) 합성 문서 fixture로 `text == body`, (b) 로컬에 `data/golden/queries/*.jsonl`과 `data/synthetic/docs/`가 있을 때만 도는 테스트는 만들지 말고(원문·로컬 파일 의존 금지), 대신 CLI `python -m kodocs.chunking.corpus --check`를 만들어 **골드·실버의 합성 qrels 전부에 대해 `sha256(text[start:end]) == span_sha256`인지 전수 검사**해 불일치 건수를 출력·종료 코드로 반영한다. 이 step에서 CLI를 로컬에서 실행해 불일치 0건을 확인한다.
6. **LoadReport**: 조건별 문서 수(합성/실제 현행/개정 전), 별표 fallback 수, 구간 0개 문서 수, 식별자 종류별 추출 문서 수, skipped 목록. 무엇을 세는지 필드 이름에 드러낸다(FP-043).

## Acceptance Criteria

```bash
make verify
uv run python -m kodocs.chunking.corpus --check
test -f data/manifest/m5_prior_versions.json
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - ARCHITECTURE.md 디렉토리 구조를 따르는가?
   - ADR 기술 스택(OpenSearch 직접 호출, BGE-M3 로컬, LangChain 검색 추상화 미사용)을 벗어나지 않았는가?
   - AGENTS.md CRITICAL 규칙(권한 pre-filter, golden 불변, 원문 비커밋, LLM 미호출)을 위반하지 않았는가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(다음 step이 쓸 파일·함수 이름 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 (API 키, 외부 인증, 설치 승인, 사람 판단 등) → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- 네트워크 요청·다운로드·패키지 설치를 하지 마라. 이유: 지시에 없는 외부 접근은 사전 승인 대상이다(FP-040). 새 의존성이 필요해 보이면 `blocked`로 보고한다.
- LLM·외부 API를 호출하거나 `anthropic`·`openai`·`google.genai`·`kodocs.llm`을 import하지 마라. 이유: M5는 결정론 검색 실험 phase이며 LLM 호출이 생기면 멈추고 보고하는 조건이다.
- `.env`를 열거나 출력하지 마라. `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이며 수치를 맞추려고 바꿀 수 없다.
- `data/raw/`·`data/interim/`를 커밋 대상으로 만들지 마라(gitignore 유지). 이유: 원문이 들어 있다. manifest에는 해시·개수·id만 남기고 조문·표 원문 텍스트를 넣지 마라.
- `phases/` 아래 파일을 수정하거나 git commit을 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `src/kodocs/retrieval/`에서 `langchain`·`langgraph`·`kodocs.agent`·`kodocs.mcp_server`·`kodocs.ui`를 (상대 import 포함) import하지 마라. 이유: 검색은 측정 대상이라 추상화 없이 OpenSearch·SQLite를 직접 호출한다(ADR-009).
- 기존 테스트를 깨뜨리지 마라
- 합성 문서 `body`에 제목·접두어를 붙이거나 공백을 정리하지 마라. 이유: qrels 문자 범위와 `span_sha256`이 이 텍스트 기준이다(FP-020). 전수 검사(`--check`)로 강제한다.
- pypdf 조문 영역·업스테이지 표 텍스트·법제처 조문을 정제·정규화해 저장하지 마라. 이유: 원문 필드를 조용히 바꾼 실패다(FP-020). 정규화는 매칭용 키에만 쓴다.
- 대상 수를 "95개"로 하드코딩해 단언하지 마라. 이유: 수량 기준은 무엇을 세는지까지 적는다(FP-043) — 로드된 수와 skipped를 함께 보고한다. 개정 전 5건은 5건이 모두 있어야 한다.
- 문서 하나의 로드 실패로 전체를 중단하지 마라(FP-030). 단 로드된 문서가 0건이면 실패로 멈춘다(FP-031).
- `kodocs.synth`의 생성 로직(LLM 호출 경로)을 import하지 마라. `kodocs.synth.identifiers`만 쓴다. 이유: 이 phase에 LLM 호출이 생기면 안 된다.
- append-only manifest(`version_matches.jsonl`, `parse_runs.jsonl`)를 줄 그대로 읽지 마라. 이유: 재실행 중복 줄이 집계를 부풀린다(FP-032). `latest_records(key=…)`로 읽는다.
