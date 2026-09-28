# Step 6: split

## 읽어야 할 파일

먼저 아래 파일들을 읽고 프로젝트의 아키텍처와 설계 의도를 파악하라:

- `docs/ADR.md` — ADR-013(dev/test, 보고는 test만), ADR-015, **ADR-025**(그룹 단위 분리, 유형별 test/dev 할당 표, 실버 60/40), **ADR-026**
- `docs/results/hypotheses.md` — 판정 대상 문항 수(검색 지표 대상 21, H5 묶음 13, H7은 split 확정 후 고정)
- step 1: `src/kodocs/eval/golden.py`(`Question.split`, `GOLD_SPLIT_QUOTA`, `ATTENTION_CHECK_PREFIX`)
- step 2 산출물 형식: `data/synthetic/queries/specs.jsonl`, step 5 산출물 형식: `data/synthetic/queries/real_projection.jsonl`

## 배경

dev(튜닝용)와 test(보고용)를 문서 단위 누수 없이 나눈다. 관련 문서가 겹치는 문항은 같은 쪽에 있어야 한다. 가설 판정 문항 수가 유형별 test 할당에 묶여 있으므로 할당을 정확히 지키고, 못 지키면 조용히 넘어가지 않고 보고한다. 이 step은 split 함수와 보고만 만든다. 실제 골드 확정 파일에 split을 쓰는 것은 step 7의 확정(promote) 명령이 이 함수를 호출해서 한다.

## 작업

TDD: `tests/eval/test_golden_split.py`를 먼저 작성한다.

### `src/kodocs/eval/golden_split.py`

```python
SPLIT_SEED_OFFSET = 404   # 회사 random_seed(data/seed/company.yaml)에 더해 쓴다
def question_groups(questions: Sequence[Question]) -> list[list[str]]: ...
    # union-find. 합성: 관련도 2 qrel doc_id를 공유하면 같은 그룹. 실제: qrel doc_id의 rule_id(serial → rule_id는 regulation_refs·law 메타데이터로 해석)가 같으면 같은 그룹.
    # 관련도 1까지 묶으면 그룹 하나가 거대해질 수 있어 관련도 2만 쓴다(ADR-025).
def assign_split(questions: Sequence[Question], quota: Mapping[str, Mapping[str, int]] | None, *, random_seed: int, test_share: float = 0.6) -> tuple[dict[str, str], dict]: ...
    # quota가 있으면(골드) 유형별 test/dev 수를 정확히 맞추도록 그룹을 배정, 없으면(실버) 유형별 round(n*test_share)를 test로.
    # 반환: ({question_id: "dev"|"test"}, report)
def split_report(questions, assignment) -> dict: ...
    # {by_type: {type: {test, dev}}, quota_diff, groups: {n, max_size, size_hist}, cross_split_docs: {n, doc_ids(최대 20개)},
    #  retrieval_test_n(= qrels가 있는 identifier·semantic·table·multi_doc·temporal test 수), h5_bundle_test_n(identifier+sql+multi_doc test 수),
    #  h7: {synthetic_n, real_n}(test 중 identifier·semantic·multi_doc의 출처별 수), temporal_tags: {tag: {test, dev}}}
```

**배정 규칙(핵심)**
- 결정론: 같은 입력·같은 시드 → 같은 배정(테스트 필수). 난수는 `random.Random(company_random_seed + SPLIT_SEED_OFFSET)`만 쓴다.
- 그룹을 쪼개지 않는다. 그룹 크기 때문에 할당을 정확히 못 맞추면 **가장 가까운 배정**을 고르고 `quota_diff`에 차이를 적고 경고를 반환한다(예외로 멈추지 않음). 그룹 하나가 너무 커서(예: 전체의 30% 초과) 할당이 불가능하면, 그 그룹 안에서만 관련도 2 문서가 가장 적게 겹치는 경계로 나누고 `cross_split_docs`에 겹친 문서를 모두 기록한다. 겹침을 0으로 만들려고 문항을 버리지 마라.
- 점검용 더미(`chk-` 접두사)가 입력에 있으면 `ValueError`로 멈춘다(골드 split에 절대 들어가면 안 됨).
- `temporal` 태그(`revision`·`as_of_event`)별 test/dev 수를 따로 보고한다.

### 오프라인 미리보기 명령

- `python -m kodocs.eval.golden_split preview`: `data/synthetic/queries/specs.jsonl`(+ 있으면 `real_projection.jsonl`)로 골드·실버 배정을 **미리보기**만 하고 `data/manifest/golden_split_preview.json`에 보고를 쓴다(결정론). 골드 확정 파일은 만들지 않는다. 실제 골드 20문항이 아직 없으면 합성 부분만 보고하고 그 사실을 보고에 적는다.
- Makefile `golden-split-preview`, AGENTS.md: `make golden-split-preview  # dev/test 배정 미리보기(그룹·할당 차이·누수 문서·가설 판정 문항 수) → data/manifest/golden_split_preview.json (네트워크 없음)`

### 테스트(필수)

- 두 문항이 관련도 2 문서를 공유하면 항상 같은 split.
- 골드 할당 표를 정확히 맞출 수 있는 fixture → `quota_diff` 전부 0, test 30·dev 20.
- 큰 그룹 때문에 못 맞추는 fixture → 가장 가까운 배정, `quota_diff`·경고 기록, 예외 없음.
- `retrieval_test_n`, `h5_bundle_test_n`, `h7.synthetic_n/real_n` 계산이 정의대로(유형 목록을 테스트에 명시).
- `chk-` 문항이 섞이면 `ValueError`.
- 같은 입력 두 번 → 배정·보고 바이트 동일.

## Acceptance Criteria

```bash
make verify
make golden-split-preview && uv run python -c "import json;d=json.load(open('data/manifest/golden_split_preview.json'));print({k:d[k] for k in d if k!='cross_split_docs'})"
grep -n "golden-split-preview" Makefile AGENTS.md
```

## 검증 절차

1. 위 AC 커맨드를 실행한다.
2. 아키텍처 체크리스트를 확인한다:
   - 할당 수치가 `golden.GOLD_SPLIT_QUOTA`(ADR-025 표)에서만 오는가(FP-043)?
   - 문서 겹침·할당 이탈이 숨겨지지 않고 보고되는가?
   - 결정론(시드 고정)인가?
3. 결과를 최종 응답(JSON)으로 보고한다. `phases/` 아래 파일은 수정하지 않고 git commit도 하지 않는다:
   - 성공 → `"status": "completed"`, `"summary": "산출물 한 줄 요약(미리보기의 그룹 수·최대 그룹 크기·누수 문서 수 포함)"`
   - 해결할 수 없는 실패 → `"status": "error"`, `"reason": "구체적 에러 내용"`
   - 사용자 개입 필요 → `"status": "blocked"`, `"reason": "구체적 사유"` 후 즉시 중단

## 금지사항

- `data/golden/`에 split이나 문항 파일을 쓰지 마라. 이유: 골드 확정은 step 7의 사용자 실행 명령만 한다(AGENTS.md).
- 할당을 맞추려고 문항을 버리거나 유형을 바꾸지 마라. 이유: 가설 판정 문항 수가 유형 할당에 묶여 있다(ADR-026).
- `docs/results/hypotheses.md`를 수정하지 마라. H7 판정 문항 수는 사람이 split 확정 결과를 보고 채운다. 이유: ADR-026(가설 파일은 사람이 고정).
- 네트워크 요청을 하지 마라. 이유: FP-040.
- 기존 테스트를 깨뜨리지 마라.
