# M3 합성 코퍼스 품질 결과

합성 문서의 생성·검증·품질 지표를 커밋된 manifest와 레지스트리만으로 보고한다. 생성 영역 밖의 사람 해석과 콘솔 기록은 갱신 시 보존한다.

<!-- BEGIN generated -->

## 1. 실행 정보

| 항목 | 값 |
| --- | --- |
| 실행일 | 2026-09-29 |
| 커밋 해시 (`git rev-parse --short HEAD`) | 0351e7f |
| 생성 모델·버전 | claude-sonnet-5 |
| generation 기록 | model=claude-sonnet-5, temperature=null (모델 미지원: unsupported_by_model), thinking=disabled, effort=null, max_tokens=6000 |
| 프롬프트 템플릿 해시 | 0e4a2386f8efccee8a5214861b45d7811efb4c00979270144191d472316630c5<br>208e83bc44a971e490a4c96cc8648d7ff71ce18bf17ea97080c069862095ee07<br>6d6499ecfc15e0355caba0a70fac1f1a5d7f041fd2cb8c8522b70fbc1c51a67e<br>8196fa38d17b04a7f4dbe3edb649a4606ce1b7d49914a4b866df71b4b7748772<br>b077ed0739e21b98cea6c8667b9bc24338ec1932f42c15fb6c4c51297fe2af79<br>e896c8ba882f6742896ed9215323a4f82d624aa23c1314065198acd941611d54<br>ea60cea81c224af1be7e699121b98779a236895d21b8d0548c666a49fbc46210<br>f127f79ae5ffee73d31c5c470bb3454927b5242e2020e1441b4ee4b481a95883<br>f2a856cb7f36eb28a2626539fe2c262546ab9a85bc9ce686e195db7cbf30478f |
| 시드 파일 해시 | data/seed/banned_names.yaml=8a41f838f46c85826b72157e6aa2aa8d7dcd62029046e85212be629dfa904231<br>data/seed/company.yaml=36c4f0caf022328db9e2cf80f23d03d681041b78f041d09c7141d7eab71e7dba<br>data/seed/events/data-ai.yaml=677b072b51ef00df617630b9b1329dff71a788e521794d3b4126df7e08ebbc3f<br>data/seed/events/finance.yaml=7703b804c3db034a6efa3bdfec77133db7d6d0ff670ffbcc7088e91ccb7552cf<br>data/seed/events/hr.yaml=6b277a26d9fe74e87761b0c7f0c16080d80c3ae06a87994a75959228c18adb45<br>data/seed/events/legal.yaml=9272b7dcedcb4059f0187ca5f41110fc4e895c117d7681dc289b696fba2aaf11<br>data/seed/events/payments.yaml=2d5b0dd9a98e0dcb97756eb643f09bbf92283adb2a01235177b99aebe645edb3<br>data/seed/events/platform.yaml=1c9adaa7940c058a2978f17b88ceb2a81806a849862e35cdfd61023f3fe99f47<br>data/seed/events/security.yaml=75dfb1ca3036419ebeb598b63e6b6cd05631c30465eed9495b79228af65044ff<br>data/seed/events/sre.yaml=6fe225cfa4086f0496ad95f0fdbe00854206b15bc4568776d4d60dde61b777c4<br>data/seed/incidents/INC-2025-001.yaml=edb517e65dedf39a3965eff54399b6fee344076e4887032be29ce4d838a3b8f0<br>data/seed/incidents/INC-2025-002.yaml=e0948645fc73aff2766ae47ff5b7da60a1add005f42a3c10a07f52c95b0e0414<br>data/seed/incidents/INC-2025-003.yaml=7f533077854d0fa5bf712e55711492018be0363f1ecf0ba23200704fbfdab1bc<br>data/seed/incidents/INC-2025-004.yaml=be7d9d0cf4de53fc22d55d24eb275ec3a9b179165207a724046195f937e343ff<br>data/seed/incidents/INC-2025-005.yaml=ba3269446e781608e006b8600594c14f2be38a2e92046bcaf10646e21c040865<br>data/seed/incidents/INC-2025-006.yaml=dd6d5402d12f3037983be4361bc53504d41538b1334cb2043eeeee801a6a42e3<br>data/seed/incidents/INC-2025-007.yaml=a0dff711e6fbd41476410c2475e7748db23bc01da11faca6ca085c2f36a55abc<br>data/seed/incidents/INC-2025-008.yaml=da7802c756e7c521d80d819f07312e490d34b2ec746e9fdbf1fc3104a76c463f<br>data/seed/incidents/INC-2025-009.yaml=becebcf6de95045c506ad6125841ec70abca10b46f8c50e4e44e428e095b167c<br>data/seed/incidents/INC-2025-010.yaml=2694091db8e8a3720e84cf52371226ff3b0bf8ac6d6ee81a1be55e803b6f620e<br>data/seed/incidents/INC-2026-001.yaml=83360eb565041fccb60664c8f2574ecdbcaad1c4beae33e962d47afde24385da<br>data/seed/incidents/INC-2026-002.yaml=a3c69728aafff311cb9be772d32da9303480e5b9d9d7ce1a0b0a10f7df7522ae<br>data/seed/incidents/INC-2026-003.yaml=09cdcfa63b0bd6ca41fbeec224e1a56f26203d033ee0b54b9e8de3d3c5998be5<br>data/seed/incidents/INC-2026-004.yaml=c0eed70c05cf25ee4457ae083ae238cfc324c329a75998537dc9deefd18f2c2d<br>data/seed/incidents/INC-2026-005.yaml=266f0b0ef8b00adeb077c6220b47190267ed9d3cf1ed3b7b01b45042e35489f5<br>data/seed/incidents/INC-2026-006.yaml=5d2f455a2865f30a4f5536eba6bb80bfc4c3febb3b804aa3449561271f661a8e<br>data/seed/incidents/INC-2026-007.yaml=4dbdea6fb6e71b68320753e2b3b4a5d0eee6a9d0668b16c12fb801b125143db3<br>data/seed/incidents/INC-2026-008.yaml=1fa6439336ab7e7bbe691083156b11bd353b12c36da788626be87c0ff0b1a5d1<br>data/seed/incidents/INC-2026-009.yaml=01d642451c6b08bd6b6b498d59824df119d70aaddf635da6767a29968d0752a7<br>data/seed/incidents/INC-2026-010.yaml=4ff4bf4df3614981f3c01251d0228a41606329423c47dd48232a996c908a95a4<br>data/seed/incidents/INC-2026-011.yaml=988e1071452315643698c45341f47ebaad30cce5bd9422e92b2cc6cf86224e97<br>data/seed/incidents/INC-2026-012.yaml=95ee1c1a76d50b0c95821a08aa67ddc54f5f4c6905008f75cc3c5fb8ce991b9a<br>data/seed/incidents/INC-2026-013.yaml=acb69deb03a19e583b0fedfccde3b54e34eaaa6523fd31de3068a892c147ea15<br>data/seed/incidents/INC-2026-014.yaml=6dd14ccfb4abcf51d04ba647f548c687447350954378fe2033abef6e1fd21b76<br>data/seed/incidents/INC-2026-031.yaml=6d2bd256ef62c110e48c151ee7a9bab24ec19111292e2ba99fa96383ea32e6e4<br>data/seed/ledger/approvals_anchor.yaml=2520b54675437b37b0f6fbfe957f86ddab03bc073ecddefdd2a13a7eab68aad6<br>data/seed/ledger/rules.yaml=5d4cb77fd325c3d81238f49d825a5e3c5f652872afd6a01d5b2de930bc4fc51b<br>data/seed/ledger/tickets_anchor.yaml=86e5122a0437e555dba69bb0a9619bf41c86d5b3a85a2b100c370a13c13fc86b<br>data/seed/people/data-ai.yaml=a8bebeacdf8a6c48549cd8b5a6e9b7c3eb03db1f845f01ee305186dacd9d7f74<br>data/seed/people/finance.yaml=9bf39c1f3ba59c37a89962e633c7bc746329b13494b13c78dba2a525595ad248<br>data/seed/people/hr.yaml=7a17f8558fa6b780ac9939863f7796c115c8243c16b978a27a56feee2435a9f7<br>data/seed/people/legal.yaml=e25dd179477fc3e3d117b9b7eaa1c5c3c502ff35354fa98a6914bc0100cf63e7<br>data/seed/people/payments.yaml=b5ce8e558a3a081820c96ba23fc116fc7ae027eb9acd61cad026d014385017d9<br>data/seed/people/platform.yaml=a6b09480889219d1c089a3a861fa004a5f8609f81c94ddb5eddf5b280a93138e<br>data/seed/people/security.yaml=c347e31e1946d66c3f90ed9a38e36197936a8aace14ab2d002f13843d392b5e8<br>data/seed/people/sre.yaml=79732d1c2778658ae2c73d2b5d5f5405ca24b0d39ab5252feac9a660f32f0cfa<br>data/seed/regulation_refs.yaml=159ba1b0c12c53323501ddde04805885d42db5bc58a9ccc39a0a625f603106f7<br>data/seed/services.yaml=bdd3a0028e5586a7c56c31b5e3d65c47704dc394a0c039e587f4d5e9b59c2bd7<br>data/seed/teams.yaml=e3b0a207df5f1dd8f6b3d40d82c016f832649c92311c550096d5a4fc5e49eb7f |
| 규정 색인 해시 | e9e23327bd6954eb141596ab44254f3b4f4faa69d4dd9dba4726686c82201bac |
| SQLite `content_hash` | 1d59c69de93c8923c3084b017335dfa0ec21840a0eac357b2ba4b61d5247f10e |
| Python 버전 | 3.12.10 |
| anthropic SDK 버전 | 1.8.0 |
| 측정 환경 | macOS-27.0.1-arm64-arm-64bit / machine=arm64 / processor=arm |

## 2. 고정 조건

| 조건 | 고정값 |
| --- | --- |
| 문서 계획 | 계획 200건 / 유형 guide=30, incident=25, meeting=50, notice=25, runbook=30, weekly=40 / 권한 public=90, restricted=30, team=80 |
| 인라인 사실 표시 | `<fact id="fact_id">…</fact>` 후 검증기가 span으로 변환 |
| 검증 규칙 버전 | 5 |
| 재생성 상한 | 최대 시도 3회 |
| 생성 방식 | Anthropic Message Batch 사용 |
| 품질 지표 대상 | 검증 통과 문서만 사용(분자·분모 동일 집합) |

### 템플릿 버전별 통과 문서 수

템플릿 해시(system.md + 유형 템플릿) 앞 12자 기준이다.

| 유형 | 템플릿 해시 | 통과 문서 수 |
| --- | --- | --- |
| guide | `208e83bc44a9` | 1 |
| guide | `6d6499ecfc15` | 29 |
| incident | `ea60cea81c22` | 25 |
| meeting | `0e4a2386f8ef` | 4 |
| meeting | `8196fa38d17b` | 46 |
| notice | `b077ed0739e2` | 24 |
| notice | `f127f79ae5ff` | 1 |
| runbook | `f2a856cb7f36` | 30 |
| weekly | `e896c8ba882f` | 40 |

## 3. 생성·비용

### 배치별 제출·결과·usage

| batch_id | 상태 | 제출 수 | 결과 유형 수 | usage 합 |
| --- | --- | --- | --- | --- |
| msgbatch_01RtBBxu4rZaYfgpGcGb92MP | collected | 10 | canceled=0, errored=0, expired=0, missing=0, succeeded=10 | input=19347, output=19203, cache_create=0, cache_read=0 |
| msgbatch_01WPEWG8ZgfC24tMViS2LLwj | collected | 10 | canceled=0, errored=0, expired=0, missing=0, succeeded=10 | input=19915, output=19223, cache_create=0, cache_read=0 |
| msgbatch_01RLrpyr7gp6zqma9K6oho1L | collected | 10 | canceled=0, errored=0, expired=0, missing=0, succeeded=10 | input=21425, output=19111, cache_create=0, cache_read=0 |
| msgbatch_01Qm4eX1ehsyw18ASU48iEvy | collected | 10 | canceled=0, errored=0, expired=0, missing=0, succeeded=10 | input=22177, output=18694, cache_create=0, cache_read=0 |
| msgbatch_01SHc7eNEULByVhCSyVD4GFp | collected | 191 | canceled=0, errored=0, expired=0, missing=0, succeeded=191 | input=416606, output=325611, cache_create=0, cache_read=0 |
| msgbatch_01D4aUrs24E51Gaa9bdEYxyG | collected | 31 | canceled=0, errored=0, expired=0, missing=0, succeeded=31 | input=83625, output=61701, cache_create=0, cache_read=0 |
| msgbatch_014CrGYtdjE38eego1bRPJop | collected | 1 | canceled=0, errored=0, expired=0, missing=0, succeeded=1 | input=2749, output=1612, cache_create=0, cache_read=0 |

### 원장·dry-run 대조

| 항목 | 값 |
| --- | --- |
| Anthropic 원장 합계(VAT 포함, USD) | 3.2028 |
| 콘솔 대조용 원장 합계(VAT 제외 환산, USD) | 2.9116 |
| dry-run 추정 입력 토큰 | 668317 (추정 기록 2개 배치) |
| 실제 입력 토큰 | 585844 |
| dry-run 추정 대비 실제 입력 토큰 비율 | 74.8% |
| 파일럿 보정 근거 | 파일럿 원장이 dry-run 예상 비용의 약 67%(3차 $0.1287/$0.19, 4차 $0.1272/$0.19). 전체 생성 1회차 실측 $2.2491/예상 $3.31(68%), 2회차 $0.4313/예상 $0.71(61%)로 확인. 추정은 예산 상한 판단용이라 `chars_per_token_estimate`는 보정하지 않았다. |

## 4. 검증

| 항목 | 값 |
| --- | --- |
| 계획 | 200 |
| 통과 | 200 |
| 실패 | 0 |
| 미생성 | 0 |
| 소진 | 0 |
| 통과율(통과/생성) | 100.0% |
| 실패 코드 분포 | 없음 |

### 카나리아 30·인젝션 12 확인

| 레지스트리 | 예상 | 확인 | 누설 |
| --- | --- | --- | --- |
| 카나리아 | 30 | 30 | 0 |
| 인젝션 | 12 | 12 | - |

## 5. 품질 지표

모든 비율 지표는 검증 통과 문서 집합 안에서 분자와 분모를 계산한다.

### 중복 — 글자 5-gram Jaccard

| 범위 | 최댓값 | p95 | 평균 |
| --- | --- | --- | --- |
| 전체 | 0.2100 | 0.0684 | 0.0319 |
| 유형 안 | 0.2100 | 0.0902 | 0.0585 |
| 유형 간 | 0.1157 | 0.0465 | 0.0263 |

#### 근접 중복 상위 20쌍(임계값 이상)

| doc_id A | doc_id B | 유형 A | 유형 B | Jaccard |
| --- | --- | --- | --- | --- |
| 없음 | - | - | - | - |

### 반복 문장

| 문장 앞 40자 | 문서 수 |
| --- | --- |
| 3개 이상 문서의 동일 문장 수 | 37 |
| 담당 팀: 법무컴플라이언스팀 | 11 |
| 담당 팀: 결제서비스팀 (payments) | 10 |
| 담당 팀: 데이터AI팀 (data-ai) | 10 |
| 담당 팀: 정보보안팀 | 10 |
| 담당 팀: SRE인프라팀 | 8 |
| 감사합니다. | 6 |
| 담당 팀: 인사팀 | 6 |
| 담당 팀: 재무팀 | 6 |
| 9%다. | 5 |
| \|---\|---\|---\| | 5 |

### 길이

| 유형 | 문서 | 평균 | 표준편차 | 변동계수 | 최소 | 최대 | 계획 목표 | 목표 대비 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| guide | 30 | 1612.7667 | 258.9571 | 0.1606 | 1235 | 2134 | 1773.3333 | 91.0% |
| incident | 25 | 1876.6800 | 419.6219 | 0.2236 | 864 | 2639 | 2896.0000 | 64.8% |
| meeting | 50 | 1760.6000 | 261.5775 | 0.1486 | 1166 | 2174 | 2216.0000 | 79.5% |
| notice | 25 | 927.8800 | 201.9830 | 0.2177 | 595 | 1324 | 1184.0000 | 78.4% |
| runbook | 30 | 1994.3667 | 284.3168 | 0.1426 | 1540 | 2481 | 2360.0000 | 84.5% |
| weekly | 40 | 1541.3750 | 345.3740 | 0.2241 | 905 | 2370 | 1880.0000 | 82.0% |

### distinct-2

| 유형 | distinct-2 | 고유 bigram | 전체 bigram |
| --- | --- | --- | --- |
| guide | 0.0811 | 3798 | 46853 |
| incident | 0.0751 | 3424 | 45580 |
| meeting | 0.0506 | 4307 | 85111 |
| notice | 0.1202 | 2664 | 22169 |
| runbook | 0.0647 | 3762 | 58188 |
| weekly | 0.0683 | 4081 | 59786 |

### 문체

| 범위 | 종결 유형 | 문장 수 | 비율 |
| --- | --- | --- | --- |
| 전체 | 합니다·했습니다류 | 845 | 11.2% |
| 전체 | 한다·했다류 | 3630 | 48.3% |
| 전체 | 함·음·명사형 | 129 | 1.7% |
| 전체 | 기타 | 2911 | 38.7% |
| guide | 합니다·했습니다류 | 344 | 27.6% |
| guide | 한다·했다류 | 380 | 30.4% |
| guide | 함·음·명사형 | 35 | 2.8% |
| guide | 기타 | 489 | 39.2% |
| incident | 합니다·했습니다류 | 53 | 5.9% |
| incident | 한다·했다류 | 549 | 61.1% |
| incident | 함·음·명사형 | 5 | 0.6% |
| incident | 기타 | 292 | 32.5% |
| meeting | 합니다·했습니다류 | 1 | 0.1% |
| meeting | 한다·했다류 | 1027 | 51.5% |
| meeting | 함·음·명사형 | 38 | 1.9% |
| meeting | 기타 | 929 | 46.6% |
| notice | 합니다·했습니다류 | 177 | 35.8% |
| notice | 한다·했다류 | 179 | 36.2% |
| notice | 함·음·명사형 | 0 | 0.0% |
| notice | 기타 | 138 | 27.9% |
| runbook | 합니다·했습니다류 | 1 | 0.1% |
| runbook | 한다·했다류 | 924 | 55.6% |
| runbook | 함·음·명사형 | 7 | 0.4% |
| runbook | 기타 | 731 | 44.0% |
| weekly | 합니다·했습니다류 | 269 | 22.1% |
| weekly | 한다·했다류 | 571 | 47.0% |
| weekly | 함·음·명사형 | 44 | 3.6% |
| weekly | 기타 | 332 | 27.3% |

### 분포

| 차원 | 값 | 계획 | 통과 |
| --- | --- | --- | --- |
| 유형 | guide | 30 | 30 |
| 유형 | incident | 25 | 25 |
| 유형 | meeting | 50 | 50 |
| 유형 | notice | 25 | 25 |
| 유형 | runbook | 30 | 30 |
| 유형 | weekly | 40 | 40 |
| 권한 | public | 90 | 90 |
| 권한 | restricted | 30 | 30 |
| 권한 | team | 80 | 80 |
| 팀 | data-ai | 23 | 23 |
| 팀 | finance | 25 | 25 |
| 팀 | hr | 25 | 25 |
| 팀 | legal | 25 | 25 |
| 팀 | payments | 27 | 27 |
| 팀 | platform | 26 | 26 |
| 팀 | security | 25 | 25 |
| 팀 | sre | 24 | 24 |
| 특수 | 카나리아 포함 | 30 | 30 |
| 특수 | 인젝션 포함 | 12 | 12 |

### 사실 커버리지

| 항목 | 값 |
| --- | --- |
| normal 사실 수 | 392 |
| 커버된 사실 수 | 392 |
| 커버리지 | 100.0% |
| 사실당 등장 문서 수 분포 | 1=99, 2=100, 3=92, 4=55, 5=32, 6=12, 7=2 |
| 2개 이상 문서에 등장한 사실 수 | 293 |

### 지시 없는 상대 시점(경고, 실패 아님)

본문에 `최근`·`얼마 전`·`지난달`·`요즘`·`요새`·`근래`가 나오지만 그 문서에 주어진 시점 표현에는 없는 경우를 문서별로 센다(프롬프트 규칙 6 위반 후보, 검증 실패로 처리하지 않음). 경고 문서 56건, 합계 74회.

| 문서 | 횟수 |
| --- | --- |
| syn-guide-004 | 최근 1 |
| syn-guide-005 | 최근 1 |
| syn-guide-009 | 최근 1 |
| syn-guide-010 | 최근 1 |
| syn-guide-011 | 최근 1 |
| syn-guide-013 | 최근 1 |
| syn-guide-016 | 최근 1 |
| syn-guide-017 | 최근 1 |
| syn-guide-021 | 최근 1 |
| syn-guide-022 | 최근 3 |
| syn-guide-023 | 최근 1 |
| syn-incident-021 | 최근 1 |
| syn-incident-025 | 최근 2 |
| syn-meeting-001 | 최근 3 |
| syn-meeting-002 | 최근 3 |
| syn-meeting-003 | 최근 1 |
| syn-meeting-004 | 최근 3 |
| syn-meeting-005 | 최근 5 |
| syn-meeting-006 | 최근 1 |
| syn-meeting-008 | 최근 3 |
| syn-meeting-009 | 최근 1 |
| syn-meeting-010 | 최근 1 |
| syn-meeting-012 | 최근 1 |
| syn-meeting-020 | 최근 1 |
| syn-meeting-021 | 최근 1 |
| syn-meeting-023 | 최근 1 |
| syn-meeting-024 | 최근 1 |
| syn-meeting-028 | 최근 1 |
| syn-meeting-029 | 최근 2 |
| syn-meeting-030 | 최근 1 |
| syn-meeting-031 | 최근 1 |
| syn-meeting-035 | 최근 1 |
| syn-meeting-036 | 최근 1 |
| syn-meeting-047 | 최근 1 |
| syn-notice-006 | 최근 1 |
| syn-notice-011 | 최근 1 |
| syn-notice-016 | 최근 1 |
| syn-notice-020 | 최근 2 |
| syn-notice-021 | 최근 1 |
| syn-runbook-005 | 최근 1 |
| syn-runbook-009 | 최근 1 |
| syn-runbook-010 | 최근 1 |
| syn-runbook-011 | 최근 2 |
| syn-runbook-012 | 최근 1 |
| syn-runbook-016 | 최근 1 |
| syn-runbook-022 | 최근 1 |
| syn-runbook-024 | 최근 1 |
| syn-runbook-026 | 최근 1 |
| syn-runbook-027 | 최근 1 |
| syn-runbook-029 | 최근 1 |
| syn-runbook-030 | 최근 1 |
| syn-weekly-001 | 최근 1 |
| syn-weekly-012 | 최근 1 |
| syn-weekly-013 | 최근 1 |
| syn-weekly-032 | 최근 1 |
| syn-weekly-033 | 최근 1 |

### 문체 경고(실패 아님)

| 항목 | 문서 | 횟수 | 유형별 문서 |
| --- | --- | --- | --- |
| 개조식 요청 종결 | 2/200 (1.0%) | 4 | guide 0/30, incident 0/25, meeting 0/50, notice 2/25, runbook 0/30, weekly 0/40 |
| 장애 보고서 티켓 번호를 문서 번호로 표기 | 5/25 (20.0%) | 5 | incident 5/25 |
| 서술체 요청 문구 | 60/200 (30.0%) | 169 | guide 23/30, incident 2/25, meeting 0/50, notice 25/25, runbook 1/30, weekly 9/40 |
| 마침표+조사 이음새 | 32/200 (16.0%) | 50 | guide 1/30, incident 1/25, meeting 13/50, notice 3/25, runbook 7/30, weekly 7/40 |
| 허용 표기 목록 누출 | 1/200 (0.5%) | 3 | guide 0/30, incident 0/25, meeting 1/50, notice 0/25, runbook 0/30, weekly 0/40 |

## 6. 티켓·결재 DB

| 테이블 | 행 수 | 전사 | 팀 | 제한 |
| --- | --- | --- | --- | --- |
| tickets | 500 | 300 | 175 | 25 |
| approvals | 150 | 30 | 90 | 30 |

## 7. 사람 검수(20건)

| 항목 | 결과 |
| --- | --- |
| 검수 표본 선정 | 20/20 (100.0%) |
| 사실 일관성 | 20/20 (100.0%) |
| 문체 자연스러움 | 평균 2.0500 / 분포 1=5, 2=9, 3=6 |
| 현실감 | 평균 2.3500 / 분포 1=2, 2=9, 3=9 |
| 인젝션 이질감 | y 3/3 (100.0%) |
| 형식 제외·누락 | 형식 제외 0행 / 누락 0건 |

### 유형별 문체·현실감

| 구분 | 검수 | 문체 평균 | 문체 분포 | 현실감 평균 | 현실감 분포 |
| --- | --- | --- | --- | --- | --- |
| guide | 3 | 1.6667 | 1=1, 2=2, 3=0 | 2.0000 | 1=1, 2=1, 3=1 |
| incident | 3 | 2.6667 | 1=0, 2=1, 3=2 | 2.6667 | 1=0, 2=1, 3=2 |
| meeting | 5 | 1.8000 | 1=3, 2=0, 3=2 | 2.4000 | 1=0, 2=3, 3=2 |
| notice | 3 | 2.0000 | 1=1, 2=1, 3=1 | 2.0000 | 1=1, 2=1, 3=1 |
| runbook | 3 | 2.3333 | 1=0, 2=2, 3=1 | 2.0000 | 1=0, 2=3, 3=0 |
| weekly | 3 | 2.0000 | 1=0, 2=3, 3=0 | 3.0000 | 1=0, 2=0, 3=3 |

### 권한별 문체·현실감

| 구분 | 검수 | 문체 평균 | 문체 분포 | 현실감 평균 | 현실감 분포 |
| --- | --- | --- | --- | --- | --- |
| public | 9 | 2.0000 | 1=2, 2=5, 3=2 | 2.4444 | 1=1, 2=3, 3=5 |
| restricted | 4 | 1.7500 | 1=2, 2=1, 3=1 | 1.7500 | 1=1, 2=3, 3=0 |
| team | 7 | 2.2857 | 1=1, 2=3, 3=3 | 2.5714 | 1=0, 2=3, 3=4 |

### 사실 오류

| doc_id | fact_id | 설명 |
| --- | --- | --- |
| 없음 | - | - |

<!-- END generated -->

## 8. 콘솔 대조(FP-037)

① 제출 전 Anthropic 콘솔 Usage/Cost를 기록한다 ② 수거 완료 후 다시 기록한다 ③ 증가분과 원장 합계(`costs.jsonl`의 anthropic 서비스, 부가세 제외 금액으로 환산)를 비교한다 ④ 차이가 있으면 `billing_margin`(현재 1.05) 조정 여부를 ADR-023에 기록한다. errored·canceled·expired는 공식 문서상 미과금이므로 원장에 없고, 콘솔에 과금이 보이면 차이 원인으로 적는다.

| 항목 | USD | 입력 토큰 | 출력 토큰 | 캐시 생성 토큰 | 캐시 읽기 토큰 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 실행 전 콘솔 Usage | (기록 없음) | (기록 없음) | (기록 없음) | (기록 없음) | (기록 없음) |
| 실행 후 콘솔 Usage(2026-09-28 조회, 합계) | 0.86 | (미기록) | (미기록) | (미기록) | (미기록) |
| 증가분 | (실행 전 기록 없음, 합계로 대조) | (미기록) | (미기록) | (미기록) | (미기록) |
| 원장 합계(VAT 제외, 수거된 배치 7개, incident-007 포함) | 2.9116 | 585,844 | 465,155 | 0 | 0 |
| 차이·차이율 | 9/29 확인: 비용 탭 집계 지연으로 판명(아래 참고), 원장 오류 아님 | - | - | - | - |

콘솔은 UTC 일자 기준이다. 원장(VAT 제외)을 배치 처리 시각(UTC)으로 나누면 반영된 날짜는 일치한다.

| UTC 일자 | 배치 | 원장(VAT 제외, USD) | 콘솔(USD) |
| --- | --- | ---: | ---: |
| 2026-09-27 | 파일럿 1·2차 | 0.231 | 약 0.23 |
| 2026-09-28 | 파일럿 3·4차 + 전체 2회차 31건 | 0.625 | 약 0.62 |
| 미반영 | 전체 1회차 191건(`msgbatch_01SHc7eNEULByVhCSyVD4GFp`) | 2.045 | - |

전체 1회차 191건 배치만 콘솔에 반영되지 않았다. 대형 배치의 과금 반영 지연으로 추정하나 확인하지 않았다. 파일럿 2차는 9/27 15:26 UTC에 제출되어 9/27로 분류했다(원장 기록 시각은 수거 시점인 9/28 01:32 UTC).

**9/29 재대조**: 콘솔 홈 월 누계 $2.99 = 원장 $2.9116(VAT 제외, incident-007 포함) + M4 질의 초안 배치(수거 전, 추정 약 $0.05). 남는 차이 약 $0.02는 M4 배치 수거 후 재확인한다. 콘솔 비용(Cost) 탭은 $0.94이며, 홈과의 차이 $2.05는 전체 1회차 191건 배치(`msgbatch_01SHc7eNEULByVhCSyVD4GFp`, 원장 $2.045)와 정확히 일치한다. 즉 비용 탭 집계가 늦게 반영될 뿐이고 원장에는 오류가 없다. `billing_margin` 조정은 불필요하다.

## 9. 한계

- 합성 문서는 실제보다 쉽다(ADR-001). 이후 검색·답변 평가는 실제 문서와 합성 문서 결과를 분리해 보고한다.
- JSON 텍스트 원본만 있고 DOCX 표·스프레드시트 같은 문서 형식 다양성 없음(ADR-024).
- 생성 모델의 샘플링 파라미터 미지원으로 동일 요청 재현 불가하며, 생성 결과 캐시로 재현성을 대체한다.

## 10. 파일럿 기록

| 차수 | 수거일 | 대상 | 통과 | 실패 원인 분류(문서 수, 한 문서가 여러 분류에 겹칠 수 있음) | 원장 |
| --- | --- | --- | --- | --- | --- |
| 1차 | 2026-09-28 (Batch `msgbatch_01RtBBxu4rZaYfgpGcGb92MP`) | 10건(6개 유형·권한 3종·카나리아 2·회고 1) | 5/10 | 표시 형식 4, 계획-검증 불일치 2, 프롬프트 누출은 통과 문서에서도 발생(작성자 person_id 9건, 경과 일수 보조 문구 3건) | 입력 19,347·출력 19,203토큰, $0.1269(VAT 포함) |
| 2차 | 2026-09-28 (Batch `msgbatch_01WPEWG8ZgfC24tMViS2LLwj`) | 1차와 같은 10건, 새 시도 | 6/10 | 설계 결함 1(배정 사실의 INC 번호를 미배정 식별자로 판정: meeting-004), 프롬프트 모호성 3(시점 표현·사실 날짜를 사실 표시로 감쌈: runbook-001·meeting-045·weekly-035), 모델 실수 2(`</fack>` 오타 1: meeting-045, 온콜 담당 표시 누락 1: weekly-035; 두 문서는 프롬프트 모호성과 겹침) | 입력 19,915·출력 19,223토큰, $0.1276(VAT 포함) |
| 3차 | 2026-09-28 (Batch `msgbatch_01RLrpyr7gp6zqma9K6oho1L`) | 1차와 같은 10건, 새 시도 | 10/10 | 검증 실패 없음. 검증 밖 품질 문제 2: "당일" 과다(계획 시점 표현 966개 중 468개가 "당일", incident-001에 "당일 2025-01-15 09:10경" 등 7회, guide-001에 기준 없는 "당일 INC-2025-007 건"), 지시 없는 "최근"(meeting-001 6회, meeting-004 5회, 규칙 6 위반) | 입력 21,425·출력 19,111토큰, $0.1287(VAT 포함) |
| 4차 | 2026-09-28 (Batch `msgbatch_01Qm4eX1ehsyw18ASU48iEvy`) | 1차와 같은 10건, 새 시도 | 10/10 | 검증 실패 없음. "당일" 11회 → 1회("당일 내 정상화", 자연스러운 용법), 지시 없는 "최근" 4건 10회(경고). 검증기 버전 4로 다시 검증하면 incident-001의 프롬프트 지시문 누출("배정된 사실 범위 내에서")이 `prompt_artifact`로 실패 | 입력 22,177·출력 18,694토큰, $0.1272(VAT 포함). 파일럿 4회 합계 $0.510(VAT 포함) |

1차 뒤 변경(검증기 버전 2): 사실 표시를 `⟦F:…⟧…⟦/F⟧`에서 `<fact id="…">…</fact>`로 교체, 계획이 준 식별자(title_hint)·규정 사실을 배정된 것으로 인정, 식별자 속 숫자를 금액으로 읽던 오탐 수정, 프롬프트에서 person_id·경과 일수 보조 문구 제거, `prompt_artifact` 실패 코드 추가. 1차 생성 캐시·원장·배치 manifest·검증 실패 기록은 지우지 않고 남긴다. 2차는 `make generate-pilot-rerun`으로 같은 10건을 새 시도로 제출하며, 최대 시도 3회는 현재 프롬프트 템플릿으로 만든 시도만 센다.

2차 뒤 변경(검증기 버전 3): 배정된 사실이 속한 장애(INC-*) 번호를 배정 식별자로 인정(그 장애의 티켓 번호처럼 배정되지 않은 다른 식별자는 계속 실패), 프롬프트에 "표시 안에는 그 사실의 값만 넣는다. 사실 날짜·시점 표현은 표시 밖에 쓴다. 한 사실 ID는 한 번만 감싼다." 규칙을 넣고 예시를 시점 표현이 표시 밖에 있는 예로 교체, 사실 줄을 `표시 안 허용 표기`와 `표시 밖 시점 표현 (사실 날짜)`로 구분. 값 없이 감싼 표시는 지금처럼 `fact_value_mismatch`로 실패한다. 계획 200건 전체 테스트에서 수정 전 105건이 INC 번호 판정으로 실패했다. 모델 실수 2건(오타, 표시 누락)은 프롬프트·검증기를 바꾸지 않는다. 3차도 `make generate-pilot-rerun`으로 같은 10건을 새 시도로 제출한다.

3차 뒤 변경: 문서 날짜와 같은 날의 사실에는 시점 표현을 주지 않는다(`fact_time_phrases`에서 제외, 계획 문서 날짜는 그대로). `plan.jsonl`을 다시 만들어 시점 표현 966개 → 498개("당일" 468개만 빠지고 날짜가 다른 사실의 표현은 그대로). 프롬프트 사실 줄은 그런 사실에 `표시 밖 사실 날짜`만 주고, 규칙 6에 "시점 표현 없이 사실 날짜만 제시된 사실은 날짜·시각을 그대로 쓰거나 시점을 생략하고 `당일` 같은 표현을 붙이지 않는다"를 넣었다. 지시 없는 상대 시점(`최근`·`얼마 전`·`지난달`·`요즘`·`요새`·`근래` 중 그 문서에 주어진 시점 표현에 없는 것)은 실패로 만들지 않고 `synth_metrics`의 `unprompted_relative_time` 경고 지표로 문서별로 세어 5절에 기록한다. M3 결과 문서는 실행일과 관계없이 이 파일 하나로 고정한다. 4차도 `make generate-pilot-rerun`으로 같은 10건을 새 시도로 제출한다.

4차 뒤 변경(검증기 버전 4): `prompt_artifact`에 프롬프트 지시문 문구 "배정된 사실"을 추가("배정된 월말 집계 단계"·"담당자 배정 예정" 같은 일반 용법은 걸리지 않음). 프롬프트는 바꾸지 않는다. 반복 문장 지표에서 "1."·"가."·"①"처럼 목록·번호 표시만 있는 조각을 제외한다(4차 상위 5개가 전부 목록 번호였다). 경고(고치지 않음): 길이 목표 대비 평균 길이가 incident 0.70, meeting 0.76으로 미달이다. 전체 생성에서도 유지되면 해석 시 감안한다. 다음은 `make generate` 전체 생성이다.

전체 생성 1회차(Batch `msgbatch_01SHc7eNEULByVhCSyVD4GFp`, 2026-09-28 수거): 191건 제출, 통과 183/200, 원장 입력 416,606·출력 325,611토큰, $2.2491(VAT 포함). 실패 17건 분류: 일시 사실 분할 11(시각만 표시로 감쌈: incident-001·005·007·008·012·016·020·024·025, meeting-030·040), ISO 값 노출 2(guide-022, meeting-008), 서술 사실 자리표시 누출 1(meeting-020), 장애 보고서 메타 서술("배정된 자료에는 수치가 명시되어 있지 않아…"; 실패 incident-006·007·024, 통과본 incident-003·004·014에도 있음). 검증 요약의 exhausted=1은 syn-incident-001로, 검증기가 시도 번호(5)를 상한 3과 비교했기 때문이다. 현재 템플릿으로 만든 시도는 4·5회차 2번뿐이라 실제로는 재시도 대상이었다.

전체 1회차 뒤 변경(검증기 버전 5): system.md에 "일시 사실은 날짜와 시각을 함께 표시 안에 쓴다. 표시 밖에 쓰는 '사실 날짜'는 사실 줄의 날짜 정보를 말하며 값 자체의 날짜가 아니다"와 일시 예시 추가(검증기는 그대로 엄격). 사실 줄의 일시 값을 허용 표기 첫 형태(예: 2026-06-14 05:35)로, 서술 사실의 허용 표기 칸을 `규칙 2의 서술 사실`로 바꿈. incident.md 영향 절 지시를 "영향받은 기능·사용자 유형을 정성적으로 쓰고, 자료나 수치가 없다는 사실은 언급하지 않는다"로 바꿈. `prompt_artifact`에 "서술 요지를 자연스럽게", "규칙 2의 서술 사실", "배정된 자료", "배정된 수치" 추가. 재생성 범위: 템플릿 해시가 바뀐 통과본은 `--regenerate-types`(Makefile `REGENERATE_TYPES`)에 적은 유형만 다시 만들고, 나머지 유형은 실패·무효화·미생성 문서만 재시도한다. system.md가 바뀌면 모든 유형의 해시가 바뀌므로, 장애 보고서만 `make generate REGENERATE_TYPES=incident`로 25건 전부를 새 템플릿으로 만든다. exhausted는 생성과 같은 규칙(현재 템플릿 시도 수)으로 세고 문서 ID(`exhausted_docs`)를 함께 기록한다. 반복 문장 지표에서 Markdown 제목 줄을 제외한다. 2절에 템플릿 버전별 통과 문서 수 표를 추가했다. 길이 미달 경고는 그대로 두고 고치지 않는다.

검수 표본(2026-09-28, `make review-sample`): 모집단 199건(incident-007 재시도 대기 중) 기준으로 추출, 이후 모집단이 바뀌어도 표본은 다시 뽑지 않음. 표본 20건 유형 guide 3·incident 3·meeting 5·notice 3·runbook 3·weekly 3, 권한 public 9·team 7·restricted 4, 부족 층 없음. 전체 2회차(msgbatch_01D4aUrs24E51Gaa9bdEYxyG, REGENERATE_TYPES=incident, 31건, $0.4313) 뒤 통과 199/200이었고, syn-incident-007 시도 3(msgbatch_014CrGYtdjE38eego1bRPJop)은 2026-09-29 수거되어 통과했다(코퍼스 200/200 확정). 표본은 모집단 199건 기준을 유지하며 `make review-sample`은 다시 실행하지 않는다.

syn-incident-007: 시도 3(`msgbatch_014CrGYtdjE38eego1bRPJop`, 2026-09-29 수거) 통과. 코퍼스는 200/200으로 최종 확정됐다.

## 11. 알려진 한계

사람 검수(20건, 2026-09-28, 판단 초안 Claude·사람 확인)는 통과본 199건(syn-incident-007 재시도 대기 중) 모집단에서 뽑은 표본을 대상으로 했다. syn-incident-007이 2026-09-29 수거·통과해 코퍼스는 200/200으로 확정됐으나 표본은 다시 뽑지 않으므로 아래 수치는 200건 기준으로 다시 집계했다(신규 문서 syn-incident-007은 아래 항목에 새로 걸리지 않았다). 재현용 카운트는 `synth_metrics`의 `style_warnings`(5절 "문체 경고")이며 실패 조건이 아니다.

| 한계 | 수치(200건 기준) | 원인·비고 |
| --- | --- | --- |
| 마침표+조사 이음새(`…했다.는`, `…되었다.고`) | 32건(16%) | 시드 서술 사실이 마침표를 포함한 완성 문장이다. |
| 허용 표기 목록 누출(`권도온 / 권도온`) | 1건 | 사실 줄의 허용 표기를 ` / `로 이어 보여준다. |
| 장애 보고서 머리말에 티켓 번호를 "문서 번호"로 표기 | 6건(25건 중) | 사람 집계. `style_warnings`는 티켓 번호만 세어 5건(나머지 1건 incident-002는 자기 장애 번호 `INC-2025-002`를 문서 번호로 썼다). syn-incident-007은 해당 없음. |
| 서술체 요청 문구(`~주시기 바랍니다`, `~하기 바란다` 등) | 60/200건, 172회. 공지 25/25·가이드 23/30. 개조식 종결(`문의 바람`, `~할 것`)은 2건뿐 | 실무 공지·가이드는 개조식이 일반적이다. `style_warnings` 패턴 기준은 60건·169회(횟수는 패턴 차이로 3회 적다). |
| 지시 없는 상대 시점(`최근` 등) | 5절 기존 경고 지표 | 프롬프트 규칙 6 위반 후보. |
| 장애 보고서 길이 | 길이 목표 대비 65% | 5절 기존 길이 지표. |

개선 후보(다음 코퍼스 버전): 시드 서술 사실에서 종결 마침표 제거, 허용 표기를 ` / ` 대신 목록으로 제시, 템플릿에 "공지·가이드의 요청·문의는 개조식" 지시.
