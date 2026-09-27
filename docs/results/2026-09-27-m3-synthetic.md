# M3 합성 코퍼스 품질 결과

합성 문서의 생성·검증·품질 지표를 커밋된 manifest와 레지스트리만으로 보고한다. 생성 영역 밖의 사람 해석과 콘솔 기록은 갱신 시 보존한다.

<!-- BEGIN generated -->

## 1. 실행 정보

| 항목 | 값 |
| --- | --- |
| 실행일 | 2026-09-27 |
| 커밋 해시 (`git rev-parse --short HEAD`) | 65e6225 |
| 생성 모델·버전 | (실행 후 기록) |
| generation 기록 | (실행 후 기록) |
| 프롬프트 템플릿 해시 | (실행 후 기록) |
| 시드 파일 해시 | data/seed/banned_names.yaml=8a41f838f46c85826b72157e6aa2aa8d7dcd62029046e85212be629dfa904231<br>data/seed/company.yaml=36c4f0caf022328db9e2cf80f23d03d681041b78f041d09c7141d7eab71e7dba<br>data/seed/events/data-ai.yaml=677b072b51ef00df617630b9b1329dff71a788e521794d3b4126df7e08ebbc3f<br>data/seed/events/finance.yaml=7703b804c3db034a6efa3bdfec77133db7d6d0ff670ffbcc7088e91ccb7552cf<br>data/seed/events/hr.yaml=6b277a26d9fe74e87761b0c7f0c16080d80c3ae06a87994a75959228c18adb45<br>data/seed/events/legal.yaml=9272b7dcedcb4059f0187ca5f41110fc4e895c117d7681dc289b696fba2aaf11<br>data/seed/events/payments.yaml=2d5b0dd9a98e0dcb97756eb643f09bbf92283adb2a01235177b99aebe645edb3<br>data/seed/events/platform.yaml=1c9adaa7940c058a2978f17b88ceb2a81806a849862e35cdfd61023f3fe99f47<br>data/seed/events/security.yaml=75dfb1ca3036419ebeb598b63e6b6cd05631c30465eed9495b79228af65044ff<br>data/seed/events/sre.yaml=6fe225cfa4086f0496ad95f0fdbe00854206b15bc4568776d4d60dde61b777c4<br>data/seed/incidents/INC-2025-001.yaml=edb517e65dedf39a3965eff54399b6fee344076e4887032be29ce4d838a3b8f0<br>data/seed/incidents/INC-2025-002.yaml=e0948645fc73aff2766ae47ff5b7da60a1add005f42a3c10a07f52c95b0e0414<br>data/seed/incidents/INC-2025-003.yaml=7f533077854d0fa5bf712e55711492018be0363f1ecf0ba23200704fbfdab1bc<br>data/seed/incidents/INC-2025-004.yaml=be7d9d0cf4de53fc22d55d24eb275ec3a9b179165207a724046195f937e343ff<br>data/seed/incidents/INC-2025-005.yaml=ba3269446e781608e006b8600594c14f2be38a2e92046bcaf10646e21c040865<br>data/seed/incidents/INC-2025-006.yaml=dd6d5402d12f3037983be4361bc53504d41538b1334cb2043eeeee801a6a42e3<br>data/seed/incidents/INC-2025-007.yaml=a0dff711e6fbd41476410c2475e7748db23bc01da11faca6ca085c2f36a55abc<br>data/seed/incidents/INC-2025-008.yaml=da7802c756e7c521d80d819f07312e490d34b2ec746e9fdbf1fc3104a76c463f<br>data/seed/incidents/INC-2025-009.yaml=becebcf6de95045c506ad6125841ec70abca10b46f8c50e4e44e428e095b167c<br>data/seed/incidents/INC-2025-010.yaml=2694091db8e8a3720e84cf52371226ff3b0bf8ac6d6ee81a1be55e803b6f620e<br>data/seed/incidents/INC-2026-001.yaml=83360eb565041fccb60664c8f2574ecdbcaad1c4beae33e962d47afde24385da<br>data/seed/incidents/INC-2026-002.yaml=a3c69728aafff311cb9be772d32da9303480e5b9d9d7ce1a0b0a10f7df7522ae<br>data/seed/incidents/INC-2026-003.yaml=09cdcfa63b0bd6ca41fbeec224e1a56f26203d033ee0b54b9e8de3d3c5998be5<br>data/seed/incidents/INC-2026-004.yaml=c0eed70c05cf25ee4457ae083ae238cfc324c329a75998537dc9deefd18f2c2d<br>data/seed/incidents/INC-2026-005.yaml=266f0b0ef8b00adeb077c6220b47190267ed9d3cf1ed3b7b01b45042e35489f5<br>data/seed/incidents/INC-2026-006.yaml=5d2f455a2865f30a4f5536eba6bb80bfc4c3febb3b804aa3449561271f661a8e<br>data/seed/incidents/INC-2026-007.yaml=4dbdea6fb6e71b68320753e2b3b4a5d0eee6a9d0668b16c12fb801b125143db3<br>data/seed/incidents/INC-2026-008.yaml=1fa6439336ab7e7bbe691083156b11bd353b12c36da788626be87c0ff0b1a5d1<br>data/seed/incidents/INC-2026-009.yaml=01d642451c6b08bd6b6b498d59824df119d70aaddf635da6767a29968d0752a7<br>data/seed/incidents/INC-2026-010.yaml=4ff4bf4df3614981f3c01251d0228a41606329423c47dd48232a996c908a95a4<br>data/seed/incidents/INC-2026-011.yaml=988e1071452315643698c45341f47ebaad30cce5bd9422e92b2cc6cf86224e97<br>data/seed/incidents/INC-2026-012.yaml=95ee1c1a76d50b0c95821a08aa67ddc54f5f4c6905008f75cc3c5fb8ce991b9a<br>data/seed/incidents/INC-2026-013.yaml=acb69deb03a19e583b0fedfccde3b54e34eaaa6523fd31de3068a892c147ea15<br>data/seed/incidents/INC-2026-014.yaml=6dd14ccfb4abcf51d04ba647f548c687447350954378fe2033abef6e1fd21b76<br>data/seed/incidents/INC-2026-031.yaml=6d2bd256ef62c110e48c151ee7a9bab24ec19111292e2ba99fa96383ea32e6e4<br>data/seed/ledger/approvals_anchor.yaml=2520b54675437b37b0f6fbfe957f86ddab03bc073ecddefdd2a13a7eab68aad6<br>data/seed/ledger/rules.yaml=5d4cb77fd325c3d81238f49d825a5e3c5f652872afd6a01d5b2de930bc4fc51b<br>data/seed/ledger/tickets_anchor.yaml=86e5122a0437e555dba69bb0a9619bf41c86d5b3a85a2b100c370a13c13fc86b<br>data/seed/people/data-ai.yaml=a8bebeacdf8a6c48549cd8b5a6e9b7c3eb03db1f845f01ee305186dacd9d7f74<br>data/seed/people/finance.yaml=9bf39c1f3ba59c37a89962e633c7bc746329b13494b13c78dba2a525595ad248<br>data/seed/people/hr.yaml=7a17f8558fa6b780ac9939863f7796c115c8243c16b978a27a56feee2435a9f7<br>data/seed/people/legal.yaml=e25dd179477fc3e3d117b9b7eaa1c5c3c502ff35354fa98a6914bc0100cf63e7<br>data/seed/people/payments.yaml=b5ce8e558a3a081820c96ba23fc116fc7ae027eb9acd61cad026d014385017d9<br>data/seed/people/platform.yaml=a6b09480889219d1c089a3a861fa004a5f8609f81c94ddb5eddf5b280a93138e<br>data/seed/people/security.yaml=c347e31e1946d66c3f90ed9a38e36197936a8aace14ab2d002f13843d392b5e8<br>data/seed/people/sre.yaml=79732d1c2778658ae2c73d2b5d5f5405ca24b0d39ab5252feac9a660f32f0cfa<br>data/seed/regulation_refs.yaml=159ba1b0c12c53323501ddde04805885d42db5bc58a9ccc39a0a625f603106f7<br>data/seed/services.yaml=bdd3a0028e5586a7c56c31b5e3d65c47704dc394a0c039e587f4d5e9b59c2bd7<br>data/seed/teams.yaml=e3b0a207df5f1dd8f6b3d40d82c016f832649c92311c550096d5a4fc5e49eb7f |
| 규정 색인 해시 | e9e23327bd6954eb141596ab44254f3b4f4faa69d4dd9dba4726686c82201bac |
| SQLite `content_hash` | 1d59c69de93c8923c3084b017335dfa0ec21840a0eac357b2ba4b61d5247f10e |
| Python 버전 | 3.12.10 |
| anthropic SDK 버전 | 1.8.0 |
| 측정 환경 | macOS-27.0-arm64-arm-64bit / machine=arm64 / processor=arm |

## 2. 고정 조건

| 조건 | 고정값 |
| --- | --- |
| 문서 계획 | 계획 200건 / 유형 guide=30, incident=25, meeting=50, notice=25, runbook=30, weekly=40 / 권한 public=90, restricted=30, team=80 |
| 인라인 사실 표시 | `⟦F:fact_id⟧…⟦/F⟧` 후 검증기가 span으로 변환 |
| 검증 규칙 버전 | (실행 후 기록) |
| 재생성 상한 | 최대 시도 3회 |
| 생성 방식 | Anthropic Message Batch 사용 |
| 품질 지표 대상 | 검증 통과 문서만 사용(분자·분모 동일 집합) |

## 3. 생성·비용

### 배치별 제출·결과·usage

| batch_id | 상태 | 제출 수 | 결과 유형 수 | usage 합 |
| --- | --- | --- | --- | --- |
| (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |

### 원장·dry-run 대조

| 항목 | 값 |
| --- | --- |
| Anthropic 원장 합계(VAT 포함, USD) | (실행 후 기록) |
| 콘솔 대조용 원장 합계(VAT 제외 환산, USD) | (실행 후 기록) |
| dry-run 추정 입력 토큰 | (실행 후 기록) |
| 실제 입력 토큰 | (실행 후 기록) |
| dry-run 추정 대비 실제 입력 토큰 비율 | (실행 후 기록) |
| 파일럿 보정 근거 | (실행 후 기록) |

## 4. 검증

| 항목 | 값 |
| --- | --- |
| 계획 | 200 |
| 통과 | (실행 후 기록) |
| 실패 | (실행 후 기록) |
| 미생성 | (실행 후 기록) |
| 소진 | (실행 후 기록) |
| 통과율(통과/생성) | (실행 후 기록) |
| 실패 코드 분포 | (실행 후 기록) |

### 카나리아 30·인젝션 12 확인

| 레지스트리 | 예상 | 확인 | 누설 |
| --- | --- | --- | --- |
| 카나리아 | 30 | (실행 후 기록) | (실행 후 기록) |
| 인젝션 | 12 | (실행 후 기록) | - |

## 5. 품질 지표

모든 비율 지표는 검증 통과 문서 집합 안에서 분자와 분모를 계산한다.

### 중복 — 글자 5-gram Jaccard

| 범위 | 최댓값 | p95 | 평균 |
| --- | --- | --- | --- |
| 전체 | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |
| 유형 안 | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |
| 유형 간 | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |

#### 근접 중복 상위 20쌍(임계값 이상)

| doc_id A | doc_id B | 유형 A | 유형 B | Jaccard |
| --- | --- | --- | --- | --- |
| (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |

### 반복 문장

| 문장 앞 40자 | 문서 수 |
| --- | --- |
| 3개 이상 문서의 동일 문장 수 | (실행 후 기록) |

### 길이

| 유형 | 문서 | 평균 | 표준편차 | 변동계수 | 최소 | 최대 | 계획 목표 | 목표 대비 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |

### distinct-2

| 유형 | distinct-2 | 고유 bigram | 전체 bigram |
| --- | --- | --- | --- |
| (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |

### 문체

| 범위 | 종결 유형 | 문장 수 | 비율 |
| --- | --- | --- | --- |
| (실행 후 기록) | (실행 후 기록) | (실행 후 기록) | (실행 후 기록) |

### 분포

| 차원 | 값 | 계획 | 통과 |
| --- | --- | --- | --- |
| 특수 | 카나리아 포함 | (실행 후 기록) | (실행 후 기록) |
| 특수 | 인젝션 포함 | (실행 후 기록) | (실행 후 기록) |

### 사실 커버리지

| 항목 | 값 |
| --- | --- |
| normal 사실 수 | (실행 후 기록) |
| 커버된 사실 수 | (실행 후 기록) |
| 커버리지 | (실행 후 기록) |
| 사실당 등장 문서 수 분포 | (실행 후 기록) |
| 2개 이상 문서에 등장한 사실 수 | (실행 후 기록) |

## 6. 티켓·결재 DB

| 테이블 | 행 수 | 전사 | 팀 | 제한 |
| --- | --- | --- | --- | --- |
| tickets | 500 | 300 | 175 | 25 |
| approvals | 150 | 30 | 90 | 30 |

<!-- END generated -->

## 7. 사람 검수(20건)

| 항목 | 결과 |
| --- | --- |
| 검수 표본 선정 | (검수 후 기록) |
| 사실 일관성 | (검수 후 기록) |
| 문체 다양성 | (검수 후 기록) |
| 현실감 | (검수 후 기록) |
| 종합 해석 | (검수 후 기록) |

## 8. 콘솔 대조(FP-037)

① 제출 전 Anthropic 콘솔 Usage/Cost를 기록한다 ② 수거 완료 후 다시 기록한다 ③ 증가분과 원장 합계(`costs.jsonl`의 anthropic 서비스, 부가세 제외 금액으로 환산)를 비교한다 ④ 차이가 있으면 `billing_margin`(현재 1.05) 조정 여부를 ADR-023에 기록한다. errored·canceled·expired는 공식 문서상 미과금이므로 원장에 없고, 콘솔에 과금이 보이면 차이 원인으로 적는다.

| 항목 | USD | 입력 토큰 | 출력 토큰 | 캐시 생성 토큰 | 캐시 읽기 토큰 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 실행 전 콘솔 Usage | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) |
| 실행 후 콘솔 Usage | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) |
| 증가분 | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) |
| 원장 합계 | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) |
| 차이·차이율 | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) | (사용자 기록) |

## 9. 한계

- 합성 문서는 실제보다 쉽다(ADR-001). 이후 검색·답변 평가는 실제 문서와 합성 문서 결과를 분리해 보고한다.
- JSON 텍스트 원본만 있고 DOCX 표·스프레드시트 같은 문서 형식 다양성 없음(ADR-024).
- 생성 모델의 샘플링 파라미터 미지원으로 동일 요청 재현 불가하며, 생성 결과 캐시로 재현성을 대체한다.
