# Step 0: m8-prereg

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `AGENTS.md` — 아키텍처·개발 프로세스 CRITICAL 규칙, 명령어 목록
- `docs/ARCHITECTURE.md` — 디렉토리 구조·패턴·계층 경계
- `docs/ADR.md` — 결정 기록 형식과 변경 기록 방식(특히 ADR-021·026·028·029·030)
- `docs/EVAL.md` — 지표·결과 보고 규칙·변경 기록
- `docs/results/hypotheses.md` — 사전 등록 형식(공통 판정 기준, M5·M6·M7 판정 규칙 절, 변경 기록 표)
- `.claude/skills/failure-patterns/SKILL.md` — 이 step에 옮겨 적은 FP 항목의 원문
- `docs/results/hypotheses.md` 전체 — 'M7 판정 규칙' 절의 형식(규칙 ID `M7-XX`, 굵은 ID + 문장, 하위 항목 들여쓰기)과 '## 변경 기록' 표를 그대로 따른다
- `src/kodocs/eval/m7_rules.py`·`tests/eval/test_m7_rules.py` — 규칙 절 추출·해시 방식(`extract_rules_section`, `rules_sha256`, `REQUIRED_RULE_IDS`)
- `src/kodocs/eval/m7_config.py` — 규칙 해시를 freeze에 기록하는 방식
- `src/kodocs/eval/m5_test.py`·`m5_stats.py` — 부트스트랩 함수(`paired_bootstrap`, `verdict`)와 시드(`_BOOTSTRAP_SEED = 20260930`)
- `docs/results/2026-09-30-m5-retrieval.md`·`data/manifest/m5_config.json`·`data/manifest/m5_eval_inputs.json` — M5 최종 구성, 검색 지표 대상 21문항 정의
- `docs/results/2026-10-02-m7-answer-eval.md`의 '지연'·'한계'·'M8 개선 후보' 절, `data/manifest/m7_latency.json`·`m7_failures.json` — 지연 규칙·d1/d2 건수
- `docs/ADR.md`의 ADR-028·029·030 형식(결정 번호 목록·이유·트레이드오프·변경 기록)

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

M8 리랭커 실험(H8)의 판정 규칙을 **결과를 보기 전에** 문서로 고정한다. 코드는 규칙 절 해시 모듈과 그 테스트뿐이다. 이 step에서 리랭커 모델을 내려받거나 실행하거나 지표를 계산하지 않는다.

### 1. `docs/results/hypotheses.md` — 'M8 판정 규칙' 절 추가
- 'M7 판정 규칙' 절 **뒤**, '## 변경 기록' **앞**에 아래 절을 문구·규칙 ID 그대로 추가한다. 이 문구가 사전 등록 원문이다. 임의로 줄이거나 바꾸지 마라. 단 `<SEED>`는 `src/kodocs/eval/m5_test.py`의 `_BOOTSTRAP_SEED` 값(20260930)을 코드에서 다시 확인해 숫자로 치환한다. 다르면 코드의 값을 쓰고 최종 보고에 적는다.
- '## 변경 기록' 표 **끝**에 행을 추가한다(기존 행은 수정하지 않는다): 날짜 `2026-10-03`, 사유 `M8 리랭커 실험 착수 전 판정 규칙 사전 등록(H8, 결과를 보기 전)`, 변경 전 `(해당 절 없음)`, 변경 후 `M8 판정 규칙 절 추가`.
- 파일 상단 설명·H1~H7 표·M5~M7 절은 한 글자도 바꾸지 않는다. 표에 H8 행을 넣지 않는다(H8은 아래 절에서만 정의).

추가할 절 원문:

`````markdown
## M8 판정 규칙 (2026-10-03 고정, M8 리랭커 결과를 보기 전)

범위: 리랭커 한 가지(H8)와 그 지연 보고. 검색 지표만 다루며 LLM 호출·답변 수준 재평가는 하지 않는다. 부트스트랩은 10,000회, 시드는 M5 test와 같은 <SEED>다. M5~M7의 확정 결과·고정 설정(m5 config_hash `455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d`)은 소급 변경하지 않으며 M8 결과는 별도 manifest·결과 문서로 기록한다.

### R. 대상·고정 조건

- **M8-R1** 대조군은 M5 최종 구성이다: 분석기 nori, `hybrid_weighted`(bm25_weight 0.7), 후보 50청크, 구조 청킹 인덱스 `kodocs-m5-composite-structural`, `prefer_current` 끔. 리랭커 입력은 이 검색이 `authorized_*`로 돌려준 후보 50청크뿐이다. 새 검색·권한 로직·후보 추가를 하지 않는다. `tools`·`search_docs` v1과 M6 고정 설정은 바꾸지 않으며, 이 실험은 평가 경로 전용이다.
- **M8-R2** 리랭커 모델은 `BAAI/bge-reranker-v2-m3`, revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`, 라이선스 Apache-2.0, `model.safetensors` 2,271,071,852바이트(fp32)다. 임베딩 모델 bge-m3와 같은 계열이다. 다른 리랭커·한국어 미세조정 파생 모델과 비교하지 않는다(변수는 리랭커 유무 하나). 로컬(MPS) 실행이며 장치는 결과에 기록한다.
- **M8-R3** 리랭커 입력은 (질문 원문, 청크 `text_nori` 원문) 쌍이다. 질문 재작성·접두어·제목 덧붙임을 하지 않는다. 점수는 순위에만 쓰며 절대값 비교·임계값을 쓰지 않는다. `predict` 호출 인자·활성화 함수는 설치된 sentence-transformers 소스에서 확인한 값으로 고정하고 결과에 기록한다.
- **M8-R4** `max_length`는 **실제 입력 쌍**(질문 + 청크 + 특수 토큰)의 토큰 길이를 리랭커 토크나이저로 재서 정한다. 대상은 dev 후보 쌍(골드 dev 검색 지표 대상 14 + 실버 dev 43문항 × 후보 50)이다. 규칙: dev 쌍 길이 P99(nearest-rank, 오름차순 `ceil(0.99n)`번째) 이상인 가장 작은 2의 거듭제곱, 상한 1024. P99가 1024를 넘으면 1024로 두고 잘린 쌍 비율을 기록한다. 상한 1024의 근거는 지연과 MPS 메모리이며, 모델 카드 권장값은 근거로 인용하지 않는다(확인한 적 없음). 측정은 모델 점수 계산 전에 하며, 값·분포·dev 잘린 쌍 비율은 `data/manifest/m8_max_length.json`에 기록한다(이 manifest가 없으면 dev 점수 계산을 거부한다). 값을 본 뒤 규칙을 바꾸지 않는다. test 쌍의 잘린 비율은 결과에 보고한다.

### H. 가설 H8

- **M8-H8** "M5 최종 구성(M8-R1)의 후보 50청크를 리랭커(M8-R2)로 재정렬하면, 골드 test 검색 지표 대상 21문항(`data/manifest/m5_eval_inputs.json`의 H1~H4 판정 대상과 같은 문항)의 문서 단위 NDCG@10이 오른다(재정렬 − M5 최종)." 문서 단위 집계·정답 집합(asker가 볼 수 있는 rel ≥ 1 문서)·이득(rel 0/1/2 선형)은 M5와 같다. 방법은 문항 짝 비교 부트스트랩이다. 판정 문구는 M5 판정 규칙과 같은 셋이다: 95% CI 하한 > 0이면 "H8 채택", 상한 < 0이면 "H8 기각(반대 방향)", 그 밖(0 포함)이면 "H8 판정 보류(차이 입증 못 함)". n=21이라 판정 보류가 나올 가능성이 높다. 그 경우에도 결과를 그대로 보고하고 동등성 주장이나 재해석을 하지 않는다.
- **M8-H8b** 보조(판정 아님): 문서 단위 Recall@10, 구간 겹침 적중률, 실제/합성 분리, 유형별 기술 통계, 실버 test 검색 지표 대상 65문항 방향 확인(같은 방법, 방향만), **순위가 나빠진 문항 수**(문항별 문서 단위 NDCG@10 차이 = 재정렬 − 대조군 < 0인 문항 수, 골드 21·실버 65 각각), 후보 50의 문서 단위 Recall@50(상한: 재정렬이 올릴 수 있는 최대치), M7 실패 분석 d1 문항 중 보이는 rel=2 문서가 후보 50에 있는 수와 재정렬 전후 순위(후보 기록이 없거나 검색 지표 대상이 아닌 문항은 '대상 외'로 센다).

### T. 튜닝·고정·실행

- **M8-T1** 튜닝은 골드 dev(검색 지표 대상 14)와 실버 dev(검색 지표 대상 43)로만 한다. 격자는 `rerank_depth ∈ {20, 30, 50}` × `fusion ∈ {단독, RRF}`의 6개다. `단독`은 상위 depth개 후보를 리랭커 점수 내림차순(동점이면 대조군 순위 오름차순)으로 재배열하고 depth 밖 후보는 대조군 순서로 뒤에 둔다. `RRF`는 depth 안의 각 후보에 `1/(60 + 대조군 순위) + 1/(60 + 리랭커 순위)`(순위는 depth 안에서 1부터)를 주어 내림차순(동점이면 대조군 순위)으로 재배열하고 depth 밖은 같다. 상위 50 후보를 쌍마다 한 번만 채점해 캐시하고 depth·fusion은 오프라인으로 계산한다. 선택 점수는 M5와 같다: (골드 dev 평균 NDCG@10 + 실버 dev 평균 NDCG@10) / 2. 동점이면 `단독`, 그다음 작은 depth. 격자·max_length 밖의 다른 설정(배치 크기 제외)을 dev에서 시험하지 않는다.
- **M8-T2** 선택 설정은 `data/manifest/m8_config.json`에 이 절의 SHA-256, m5 config_hash, 모델 revision, max_length, 선택 depth·fusion, dev manifest 해시와 함께 고정한다(덮어쓰기 거부). freeze 전에는 test를 실행하지 않는다.
- **M8-X1** test는 확정 설정으로 **한 번** 실행한다(골드 test 21 + 실버 test 65). 실행 전 순서대로 검사하고 하나라도 어긋나면 중단한다: (1) 이 절의 해시가 `m8_config.json`의 기록과 같다. (2) 인덱스 이름·`chunk_count`·`schema_hash`가 `m5_config.json`과 같다. (3) 같은 후보로 다시 계산한 대조군 문항별 NDCG@10이 M5 test run-01의 값과 일치한다. 재실행은 `RERUN_REASON`(사유·날짜·이전 결과 해시 누적)이 있어야 하며 결과 문서는 첫 실행과 재실행을 모두 보고한다. 원본 실행 jsonl의 SHA-256과 행 수는 실행 시점(완료·정지 시)에 커밋 manifest에 기록하고 이후 분석·보고 전에 현재 원본과 대조한다(FP-053).

### P. 지연

- **M8-P1** 지연은 test와 별도 실행(`make m8-latency`)에서 M7 지연과 같은 113문항(골드 test 30 + 실버 test 83)을 동시성 1로 잰다. 검색 + 재정렬만 실행하고 LLM은 호출하지 않는다. 예열 3건(dev)은 제외하고 모델 로딩 시간은 `init_s`로 분리한다. 구간은 `embed_s`·`search_s`·`rerank_s`다. 장치·배치 크기·두 모델(bge-m3 임베딩 + 리랭커)을 함께 올렸을 때의 MPS 메모리 정보는 기록만 한다.
- **M8-P2** 재정렬 구간(`rerank_s`)의 P50·P95(nearest-rank)·최대와 부트스트랩 95% CI(병기)를 기술 통계로 보고한다. 목표는 사전 등록하지 않는다. 단순 RAG 영향은 "M7 단순 RAG 전체 P95(3.732초) + `rerank_s` P95"로 보고하며, 이는 **두 P95를 단순히 더한 보수적 근사이고 분포를 합성한 값이 아니다**. M7 값은 임베딩 캐시 적중 상태에서 쟀다는 한계를 함께 적는다. 5.0초 목표 대비 여유(초)를 보고하되 판정이 아니다. M7 값은 manifest에서 읽고 문서에 손으로 옮기지 않는다.

### N. 범위 밖

- **M8-N1** 아래는 이 phase에서 하지 않으며 사유를 결과 문서에 적는다: 답변 수준 재평가(Gemini·OpenAI 재실행), small-to-big(M7 실패 분석 d2 예산 탈락이 전 시스템·세트에서 0건이라 근거가 없다), 코퍼스 v2(별표 조립 결함의 영향 문항은 골드 test 1개 g-real-007뿐이라 지표 변화를 측정할 수 없다), 리랭커의 `tools` 연동, 한국어 미세조정 리랭커 비교.

### D. 결과 표시

- **M8-D1** README·결과 문서는 H8을 세 상태로만 표시한다: 결과 파일이 있고 기록된 원본 해시·행 수 검증을 통과하면 결과, freeze만 있거나 아무것도 없으면 "미실행", 결과 흔적(결과 디렉터리·실행 기록)이 있는데 결과 파일이 없거나 검증에 실패하면 오류로 멈춘다. 실패를 "미실행"으로 숨기지 않는다.
`````

### 2. `docs/ADR.md` — ADR-031 추가
- 파일 **끝**(ADR-030 뒤)에 `### ADR-031: M8 리랭커 실험 설계`를 추가한다. 형식은 ADR-029·030을 따른다(**결정**·**이유**·**트레이드오프**·**변경 기록**). 결정에는 위 M8-R1~R4·H8·T1·T2·X1·P1·P2·N1을 번호 목록으로 요약하고 `docs/results/hypotheses.md` 'M8 판정 규칙'을 참조한다. 이유에는 M7 실패 분석에서 d1은 Agentic 골드 2건·실버 7건이고 단순 RAG는 후보 기록 부재로 indeterminate가 많아 근거가 약하다는 점, 그래서 H8을 검색 지표 실험으로만 하고 답변 개선을 주장하지 않는다는 점, `max_length` 상한 1024의 근거가 지연·메모리라는 점을 쓴다. 트레이드오프에는 n=21이라 판정 보류 가능성이 높다는 점, 대조군(구조 청킹)이 고정 청킹보다 NDCG가 낮다는 M5 사실을 그대로 둔다는 점을 쓴다. ADR-031 앞의 ADR 본문은 한 글자도 바꾸지 않는다.
- 변경 기록: `- 2026-10-03: M8 리랭커 설계 확정(결과를 보기 전)`.

### 3. `docs/EVAL.md` — 변경 기록 행만
- '## 변경 기록' 표 끝에 행을 추가한다: `2026-10-03 | M8 리랭커(H8)·지연 규칙 사전 등록(hypotheses.md 'M8 판정 규칙' 참조) | M8 착수 전 사전 등록`. 다른 곳은 바꾸지 않는다(사다리의 '+리랭커' 행은 M8 결과 문서에만 둔다).

### 4. `src/kodocs/eval/m8_rules.py` + `tests/eval/test_m8_rules.py`
`m7_rules.py`와 같은 구조로 만든다.
- `REQUIRED_RULE_IDS: tuple[str, ...]` = `M8-R1`·`M8-R2`·`M8-R3`·`M8-R4`·`M8-H8`·`M8-H8b`·`M8-T1`·`M8-T2`·`M8-X1`·`M8-P1`·`M8-P2`·`M8-N1`·`M8-D1`.
- `extract_rules_section(path=Path("docs/results/hypotheses.md")) -> str`: `## M8 판정 규칙` 헤딩부터 다음 level-2 헤딩 전까지(공백·줄바꿈 보존). 없으면 `ValueError`.
- `rules_sha256(path=...) -> str`: 추출 원문의 UTF-8 SHA-256.
- `python -m kodocs.eval.m8_rules`는 규칙 ID 수와 SHA-256만 출력한다(네트워크 없음).
- 테스트: 필수 ID가 모두 절에 있다 / 해시가 같은 입력에서 안정적이다 / 절이 'M7 판정 규칙' 뒤·'## 변경 기록' 앞에 있다 / 절에 모델 revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`·바이트 수 `2,271,071,852`·시드 값(코드의 `_BOOTSTRAP_SEED`와 같음)·`21`문항·`1024`가 들어 있다 / M7 절 해시(`m7_rules.rules_sha256`)가 이 step 전후로 달라지지 않았음을 `data/manifest/m7_config.json`에 기록된 규칙 해시와 대조한다(필드 이름은 `m7_config.py`에서 확인).
- `AGENTS.md`의 `## 명령어` 절 끝(M7 명령 뒤)에 이 step이 추가한 명령이 있으면 한 줄로 적는다(이 step은 Makefile 타깃을 추가하지 않는다).

## Acceptance Criteria

```bash
uv run python -m kodocs.eval.m8_rules   # 규칙 ID 수와 SHA-256 출력
make verify   # ruff + pytest 통과
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

- 'M8 판정 규칙' 문구를 임의로 바꾸거나 요약하지 마라(`<SEED>` 치환 제외). 이유: 결과를 보기 전에 등록한 문구가 판정의 근거다(ADR-026). 판정 문구를 바꿔야 하면 `blocked`로 보고한다.
- 리랭커 모델을 내려받거나 로드하거나, 어떤 M8 지표·지연도 계산하지 마라. 이유: 사전 등록은 결과 전이어야 한다. 모델 파일·점수가 이 시점에 없는 것이 정상이다.
- 'M5·M6·M7 판정 규칙' 절, H1~H7 표, 기존 변경 기록 행을 수정하지 마라. 이유: 확정 결과 소급 변경 금지(M7 규칙 절 해시가 `m7_config.json`에 고정돼 있다).
- 규칙에 적힌 수치(모델 revision·바이트·1024·시드·21문항)를 코드에서 확인하지 않고 추측해 바꾸지 마라. 이유: FP-011. 코드·manifest 값과 다르면 `blocked`로 보고한다.
- 수량 기준은 무엇을 세는지까지 적어라(예: '순위가 나빠진 문항' = 문항별 NDCG 차이 < 0인 문항, 'dev 쌍' = dev 검색 지표 대상 문항 × 후보 50). 이유: FP-043(모호한 수량 기준).
- `.env` 파일을 열거나 출력하지 마라. 이유: API 키 유출 방지(AGENTS.md CRITICAL). 필요한 변수 이름은 `.env.example`에만 적는다.
- `data/raw/`·`data/interim/`의 내용을 커밋하거나 수정하지 마라(읽기는 필드 이름 확인용으로만 허용). 이유: 원문·정제 산출물은 비커밋이다(ADR-002). 예외: step 6·7이 만드는 `data/interim/m8/` 캐시 쓰기만 허용되며 이 경로도 커밋하지 않는다.
- 실제 API 호출, 모델·데이터 다운로드, 패키지 설치를 하지 마라. `pyproject.toml`·`uv.lock`도 수정하지 마라(step 3의 Makefile `verify` 줄 변경은 별개). 이유: 사용자가 직접 실행·승인한다(ADR-014, FP-040). 필요한 패키지가 없으면 `blocked`로 보고한다.
- 실측 인덱스(`kodocs-m5-composite-fixed`·`-structural`, `kodocs-m5-pypdf-structural`, `kodocs-m5-upstage-structural`)에 쓰기·삭제를 하지 마라. 통합 테스트가 필요하면 uuid 붙은 임시 이름만 쓴다. 이유: FP-046(M5 실측 중 통합 테스트가 실측 인덱스를 삭제함).
- test를 `RERUN_REASON` 없이 다시 실행하거나, 실행 잠금을 우회하는 코드를 만들지 마라. 이유: 사전 등록한 1회 실행 규칙(ADR-026, M5·M6·M7 동일 규칙).
- `phases/` 아래 파일을 수정하지 마라. git commit도 하지 마라. 이유: index.json은 execute.py만 수정한다.
- `data/golden/`(질의셋·qrels·사람 채점)을 수정하지 마라. 이유: 사람이 확정한 기준이다(AGENTS.md CRITICAL). 수치를 맞추려고 golden을 바꾸지 않는다.
- M5·M6·M7의 확정 manifest와 결과 문서를 수정하지 마라(해당 step이 명시한 한 줄 참조 추가만 예외). 이유: 확정 결과 소급 변경 금지, 해시 대상이 깨진다(FP-053).
- 기존 테스트를 깨뜨리지 마라.
