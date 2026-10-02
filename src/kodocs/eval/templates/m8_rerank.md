# M8 리랭커 실험 결과

<!-- BEGIN generated -->

(사용자 실행 후 생성)

<!-- END generated -->

## 사용자 실행 절차

1. `make up`, `make m8-freeze` 완료와 로컬 M5 문항별 결과를 확인한다.
2. `make m8-test` — test는 한 번, 재실행은 `RERUN_REASON="사유" make m8-test`.
3. `make m8-latency` — 다른 부하 없이 동시성 1, LLM 호출 없음. 재실행은 `RERUN_REASON="사유" make m8-latency`.
4. `make m8-report` → `make readme` → `make m8-audit`.
5. 결과·README를 갱신한 뒤 `make verify`로 검증하고 사용자가 커밋한다.

## 한계(사용자 보존 영역)

- 검색 지표만 측정한다. H8이 채택이어도 답변 수준 효과는 측정하지 않았다.
- 실버는 방향 확인용이며 추가 가설 판정이 아니다.
