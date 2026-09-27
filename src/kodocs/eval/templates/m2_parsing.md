# M2 파싱 비교 결과

## 1. 측정 정보

manifest에서 생성한 실행일·커밋·도구 버전·설정 및 입력 해시는 아래 생성 결과표에 기록한다. 측정 환경은 실행한 사람이 채운다.

| 항목 | 값 |
| --- | --- |
| macOS 버전 | (실행 후 기록) |
| 기기·CPU/GPU | (실행 후 기록) |
| Docling 실행 장치 | (실행 후 기록) |
| 측정 중 다른 부하 여부 | (실행 후 기록) |

## 2. 권장 실행 순서

1. `make parse-pypdf`
2. `make parse-score`
3. `data/interim/parse_diffs/pypdf/`와 분할기 상한 결과로 분할 결과를 육안 확인한다. 분할기 규칙 문제면 여기서 고친다.
4. `make docling-models` → `make parse-docling`
5. `make parse-upstage-dry`로 예상 비용을 확인한다.
6. `make parse-upstage`
7. `make table-drafts` → 사람이 약 10개를 검수해 `data/golden/tables/`에 확정 → `make table-golden` → `make table-score`
8. `make parse-score` → `make parse-report`
9. (보조 실험) `make parse-upstage-direct-dry` → `make parse-upstage-direct` → `make parse-score` → `make table-score` → `make parse-report`

## 3. 고정 조건

- 입력은 M1에서 변환한 동일 PDF이고 `infra/fontconfig/fonts.conf` 해시를 기록한다.
- 조문 분할기와 채점 코드는 모든 파서에 동일하게 적용한다.
- pypdf는 plain 추출, Docling은 OCR 끔·표 구조 켬, 업스테이지는 `standard`·`ocr=auto`·표 병합 끔으로 고정한다.
- 대상은 115개(현행 95 + 과거 20)다.

## 4. 정규화 규칙과 비교 방식

원문 비교는 유니코드 NFC와 바깥 공백 제거만 적용한다. 정규화 비교는 아래 N1~N5를 순서대로 적용하며, 저장된 원문은 바꾸지 않는다.

| 규칙 | 변환 |
| --- | --- |
| N1 | 유니코드 NFC 적용 |
| N2 | 균형 잡힌 법령 편집 주석 제거 |
| N3 | 따옴표(`` " ' “ ” ‘ ’ ` ´ `` 등)를 `"` 하나로, 가운뎃점(`ㆍ · ․ ‧ ∙` 등)을 `·` 하나로 통일한 뒤 NFKC(전각 콜론·괄호·숫자 등 호환 문자, 원문자 ①→1)를 적용하고 다시 통일한다. NFKC를 먼저 하면 `ㆍ`가 조합형 자모로, `․`가 마침표로 바뀌므로 순서를 고정한다 |
| N4 | 모든 유니코드 공백 제거 |
| N5 | 그 외 문자는 변경하지 않음 |

## 5. 생성 결과표

<!-- BEGIN generated -->

(실행 후 기록)

<!-- END generated -->

## 6. 대표 실패 사례

(실행 후 기록 — 규정 번호·조 번호·차이 유형으로)

조문이나 표의 원문은 커밋 대상인 이 문서에 넣지 않는다.

## 7. 변환 경유 vs 직접 입력(업스테이지 보조 실험)

`make parse-upstage-direct-dry`로 변환 PDF 쪽수 × 1.5 가정의 예상 비용을 확인한 뒤 `make parse-upstage-direct`를 사용자가 실행한다. 이어서 `make parse-score` → `make table-score` → `make parse-report` 순서로 실행하면 위 생성 결과표에 양쪽 결과가 있는 대상만 짝지은 비교가 추가된다. 실제 HWP/HWPX 과금 쪽수는 응답 `usage.pages`이며 PDF 쪽수와 다를 수 있다.

## 8. 비용 메모

원장 금액은 **정가 + 부가세 10% 포함**이며 크레딧은 반영하지 않는다. 업스테이지 콘솔 사용량은 VAT 제외라 원장 금액과 다를 수 있다. 콘솔 월 한도($25)는 서비스 쪽 차단으로 원장 상한과 별개다.

## 9. 한계

- 검색 영향(Recall@10)은 M4 질의셋과 M5 인덱스가 필요하므로 M5로 이월한다.
- 쪽수는 대체 글꼴에 좌우된다.
- 여러 쪽 표는 엄격하게 채점한다.
- 법제처 조문과 ALIO 원본 사이에 편집 차이가 있을 수 있다.
- 법제처 본문에 조문 텍스트가 없는 버전은 생성 결과표에 파싱 대상 밖 버전까지 싣는다. M4 질의셋 작성 시 이 규정은 조항 단위 정답을 법제처에서 얻을 수 없다고 기록한다.
