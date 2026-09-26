# M1 데이터 수집 결과

## 측정 정보

실측 시 파일명을 `docs/results/{실행일}-m1-data-collection.md`로 바꾼다(EVAL.md 결과 보고 규칙).

| 항목 | 값 |
|---|---|
| 실행일 | (실행 후 기록) |
| 커밋 해시 | (실행 후 기록) |
| macOS 버전 | (실행 후 기록) |
| LibreOffice | 26.8.0.3 |
| H2Orestart | 0.7.14 |
| `infra/fontconfig/fonts.conf` sha256 | `0d89d163be5cc3dc819c0af5fe10aeb9fcbf67c97eeea8731aa61305a5e1fb83` (설정이 바뀌면 이 값이 달라진다) |
| 함초롬 글꼴 설치 여부 | (실행 후 기록) |
| 고정 조건 | `--approx-days 7`, `--history-rules 10`, `--versions-per-rule 2` |

## 1. 목적과 범위

M1은 NIA 내부규정 95개를 대상으로 M2 파싱 비교에 필요한 원문과 정답 기준을 재현 가능하게 준비한다. ADR-022에 따라 법제처 Open API에서 현행·연혁의 구조화 조문과 발령일자를 수집하고, ALIO에서 같은 규정의 원본 파일과 개정 버전을 수집하는 두 경로를 사용한다. 실제 규정은 ADR-002에 따라 기관명 치환 없이 무가공으로 로컬에서만 사용하며 원문과 정제 산출물은 커밋하지 않는다.

## 2. 재현 절차

아래 순서는 앞 단계의 manifest를 다음 단계가 입력으로 사용하므로 바꾸지 않는다. 실제 API 호출과 로컬 변환은 사용자가 수행한다.

1. `.env`에 `LAW_OPEN_API_KEY`를 입력한다. 키 값은 로그나 manifest에 기록하지 않는다.
2. `make collect-law`를 실행한다. 법제처 응답 원본을 `data/raw/law/{rule_id}/{serial}.json`, 개인정보를 제거한 구조화 조문을 `data/interim/law/{rule_id}/{serial}.json`, 출처·해시·버전 메타데이터를 `data/manifest/law_api.jsonl`에 만든다.
3. `make collect-alio`를 실행한다. 직접 첨부와 안전하게 해제한 ZIP 멤버를 `data/raw/alio/` 아래에 저장하고 `data/manifest/alio.jsonl`과 다운로드·해제 실패 기록 `data/manifest/alio_failures.jsonl`을 만든다.
4. `make match-versions`를 실행한다. 두 수집 manifest만 읽어 `data/manifest/version_matches.jsonl`을 매번 새로 쓴다.
5. `make convert-hwp`를 실행한다. 명령 시작 시 LibreOffice·H2Orestart 설치를 점검하고 `infra/fontconfig/fonts.conf`의 글꼴 대체 규칙을 적용한다. 변환 PDF는 `data/raw/pdf/`, 변환 결과와 쪽수·글꼴 정보는 `data/manifest/conversions.jsonl`에 기록한다.
6. `make parse-targets`를 실행한다. 네 수집·매칭·변환 manifest만 읽고 M2 입력 목록과 예상 페이지 수를 `data/manifest/parse_targets.json`에 새로 쓴다.

## 3. manifest 설명

| 파일 | 한 줄 설명 | 핵심 필드 |
|---|---|---|
| `data/manifest/law_api.jsonl` | 법제처 현행·연혁 본문 수집 이력 | `rule_id`, `serial`, `name`, `issued_date`, `effective_date`, `is_current`, `article_count`, `annex_count`, `annex_titles`, 원본·정제 경로와 SHA-256 |
| `data/manifest/alio.jsonl` | ALIO 직접 첨부와 ZIP 멤버의 출처·형식 이력 | `source`, `rule_no`, `rule_title`, `file_no`, `file_name`, `file_kind`, `filename_date`, `zip_member_path`, `local_path`, `sha256` |
| `data/manifest/alio_failures.jsonl` | 규정 조회·다운로드·ZIP 해제 실패 이력 | `rule_no`, `rule_title`, `file_no`, `file_name`, `reason`, `collected_at` |
| `data/manifest/version_matches.jsonl` | ALIO 파일과 법제처 버전의 결정론적 매칭 결과 | `alio_file_no`, `alio_local_path`, `alio_sha256`, `law_rule_id`, `law_serial`, `law_issued_date`, `confidence`, `basis`, `candidates`, `parse_eval_eligible` |
| `data/manifest/conversions.jsonl` | HWP/HWPX별 PDF 변환 결과와 품질 점검 메타데이터 | `source_sha256`, `source_kind`, `output_path`, `page_count`, `status`, `error`, `embedded_fonts`, `hangul_distinct`, 도구 버전 |
| `data/manifest/parse_targets.json` | M2 파싱 대상과 예산 산정을 위한 쪽수 목록 | 선택 파라미터, 현행 누락 목록, `role`, 법제처 식별자·발령일자·별표 수, ALIO 경로·해시·형식, PDF 경로·쪽수, `embedded_fonts`, `unexpected_fonts` |

`data/manifest/`의 메타데이터는 커밋 대상이다. 원문을 담는 `data/raw/`와 법제처 정제 조문을 담는 `data/interim/`은 커밋하지 않는다. manifest에는 원문 텍스트나 담당자명·전화번호를 넣지 않는다.

## 4. 버전 매칭 규칙

규정명은 기관명 접두사, 공백, 가운데점 변형을 제거해 정규화하며, 하나의 법제처 `rule_id`로만 결정될 때 파일 날짜를 비교한다. 같은 날짜의 후보가 여러 개면 임의로 고르지 않는다.

| 신뢰도 | 판정 규칙 | 파싱 평가 |
|---|---|---|
| 정확(`exact`) | 파일명의 일 단위 날짜가 법제처 버전의 발령일자 또는 시행일자와 정확히 일치하고 후보가 하나 | 포함 가능 |
| 근사(`approximate`) | 일 단위 날짜가 발령·시행일자의 ±7일 안에서 후보 하나이거나, 월 단위 날짜에 발령된 후보가 하나이거나, 날짜 없는 같은 규정의 최신 직접 파일이 ALIO 최종 개정일로 후보 하나와 대응 | 포함 가능 |
| 실패(`failed`) | 규정명이 없거나 모호함, 후보가 0개 또는 여러 개, 지원하지 않는 날짜 정밀도, 날짜 없는 비최신 파일 등으로 버전을 하나로 확정하지 못함 | 제외(`parse_eval_eligible=false`) |

동일 법제처 버전에 ALIO 파일 여러 개가 매칭되면 근거에 중복 사실을 남긴다. 실패 파일은 파싱 평가 대상에 절대 넣지 않는다.

## 5. 파싱 대상 선정 규칙

- 현행본은 법제처 `is_current=true`인 모든 버전을 대상으로 한다. `parse_eval_eligible=true`인 매칭 파일 중 HWPX를 HWP 5.x보다 먼저 고르고, 형식이 같으면 `file_no`가 큰 파일을 고른다. eligible 파일이 없으면 조용히 제외하지 않고 `missing_current`에 규정명과 이유를 기록한다.
- 과거본은 법제처 연혁 버전이 3개 이상이며 과거 eligible 파일이 있는 규정을 연혁 버전 수 내림차순, `rule_id` 오름차순으로 정렬한다. 기본적으로 앞의 10개 규정에서 가장 오래된 과거 버전과 가장 최근 과거 버전을 고른다. 한 버전만 고를 때는 가장 최근 과거 버전을 고른다.
- 변환 성공 기록은 원본 `source_sha256`으로 연결한다. 최신 변환 기록이 `status=ok`가 아니거나 기록이 없으면 `page_count=null`인 "변환 전" 대상으로 집계한다.
- `make parse-targets`는 기본값 `--history-rules 10 --versions-per-rule 2`를 사용한다. 사용자는 업스테이지 페이지 예산에 맞춰 두 옵션을 낮추거나 높일 수 있다.
- 최종 대상은 `(role, rule_no, issued_date)` 순으로 정렬하며 원문 텍스트나 담당자 정보는 담지 않는다.

## 6. ALIO HWP 안의 별표 포함 여부 확인 방법

이 확인은 M2 파서 비교 전에 수행한다. M1에서는 다음 방법만 확정하며 별표 판정 도구나 정답은 만들지 않는다.

1. **기준 목록을 만든다.** 해당 버전의 법제처 정제 JSON `data/interim/law/{rule_id}/{serial}.json`에서 `annexes`를 읽는다. 각 항목의 별표번호(`number`), 가지번호(`branch`), 구분(`kind`: 별표/별지), 제목(`title`)이 그 버전에 있어야 할 기준이다. `parse_targets.json`의 `law_annex_count`가 0인 대상은 확인할 필요가 없다.
2. **별도 첨부 여부를 확인한다.** 같은 규정의 ALIO `bFiles`에서 온 파일명, 즉 `alio.jsonl`의 `file_name`에 `별표`, `별지`, `서식`이 있으면 본문 HWP와 별도로 첨부된 것으로 본다.
3. **HWPX 본문을 확인한다.** HWPX ZIP의 `Contents/section*.xml`을 열고 `hp:t` 요소의 텍스트에서 `[별표 N]`, `별표 N`, `[별지 제N호` 패턴과 법제처 `annexes`의 제목 문자열을 찾는다. 표 존재 여부는 `hp:tbl` 요소 수로 확인한다.
4. **HWP 5.x 본문을 확인한다.** M1 형식 판정 의존성인 `olefile`로 OLE 스트림 `BodyText/Section{N}`을 읽는다. `FileHeader` 속성의 압축 비트가 켜져 있으면 raw deflate이므로 `zlib.decompress(data, -15)`로 푼다. 각 레코드의 4바이트 헤더에서 태그 ID는 하위 10비트, 크기는 상위 12비트로 읽고, 크기가 `0xFFF`이면 다음 4바이트를 실제 크기로 읽는다. `HWPTAG_PARA_TEXT`(태그 67)의 UTF-16LE 텍스트에서 HWPX와 같은 패턴 및 법제처 별표 제목을 찾고, `HWPTAG_TABLE`(태그 77)로 표를 센다. `PrvText` 스트림은 문서 앞부분 미리보기만 있으므로 사용하지 않는다.
5. **대체 확인 경로를 남긴다.** HWP 레코드 파싱 구현 부담이 크면 변환 PDF를 `pypdf`로 읽어 같은 패턴과 제목을 찾을 수 있다. 다만 변환 과정에서 텍스트나 표가 손실될 수 있어 "찾지 못함"은 원본 부재의 확정 근거로 쓰지 않는다.
6. **판정하고 M2 후보를 고른다.** 법제처 별표 항목마다 `본문 포함` / `별도 파일` / `찾지 못함`으로 분류한다. 이 결과에서 표 구조와 난도가 다양한 약 10개를 M2 표 셀 일치율의 사람 검수 정답 후보로 고른다.

## 7. 결과 수치

수집·변환·선정 명령을 사용자가 실행한 뒤 아래 칸을 manifest에서 집계해 기록한다.

| 항목 | 값 |
|---|---|
| 법제처 현행 버전 수 | (실행 후 기록) |
| 법제처 연혁 버전 수 | (실행 후 기록) |
| ALIO 규정 수 | (실행 후 기록) |
| ALIO 파일 수: 전체 | (실행 후 기록) |
| ALIO 파일 수: HWP 5.x(`hwp5`) | (실행 후 기록) |
| ALIO 파일 수: HWPX(`hwpx`) | (실행 후 기록) |
| ALIO 파일 수: HWP 3.x(`hwp3`) | (실행 후 기록) |
| ALIO 파일 수: 기타 OLE(`ole_other`) | (실행 후 기록) |
| ALIO 파일 수: ZIP(`zip`) | (실행 후 기록) |
| ALIO 파일 수: PDF(`pdf`) | (실행 후 기록) |
| ALIO ZIP 멤버 수 | (실행 후 기록) |
| 다운로드 실패 수 | (실행 후 기록) |
| 매칭 파일 수: 정확(`exact`) | (실행 후 기록) |
| 매칭 파일 수: 근사(`approximate`) | (실행 후 기록) |
| 매칭 파일 수: 실패(`failed`) | (실행 후 기록) |
| 변환 성공 수 | (실행 후 기록) |
| 변환 실패 수 | (실행 후 기록) |
| 예상 외 글꼴이 섞인 파일 수 | (실행 후 기록) |
| `hangul_distinct=0`인 파일 수 | (실행 후 기록) |
| 파싱 대상 수: 전체 / 현행 / 과거 | (실행 후 기록) |
| 파싱 대상 예상 페이지 수 / 페이지 수 미상 대상 수 | (실행 후 기록) |

예상 외 글꼴이 섞인 파일 수는 `conversions.jsonl`을 `source_sha256`별 마지막 줄로 축약한 뒤, 최신 `status=ok` 레코드의 `embedded_fonts` 중 예상 글꼴(`HCR*` 함초롬, `Liberation*` 라틴 문자 부분 대체)이 아닌 이름이 하나라도 있는 파일을 센다. 글꼴 이름별 파일 수도 아래에 중복 없이 집계한다.

| 섞인 예상 외 글꼴 이름 | 파일 수 |
|---|---|
| (실행 후 기록) | (실행 후 기록) |

## 8. 한계

- 원본이 지정한 휴먼명조·HY견고딕 등 상용 또는 Windows 글꼴은 `infra/fontconfig/fonts.conf` 규칙에 따라 함초롬(`HCR Batang`, `HCR Dotum`)으로 대체된다. 함초롬을 설치하고 대체 규칙을 적용했을 때 샘플 2건의 쪽수는 GUI 변환과 같았다. 라틴 문자 부분은 LiberationSerif 등으로 대체되며 이는 정상이다. 대체 규칙에서 빠진 글꼴이 있으면 `embedded_fonts`에 예상 외 글꼴(`HCR*`·`Liberation*` 외)이 섞여 드러나며, `parse_targets.json`의 `unexpected_fonts`로 M2에서 제외 또는 별도 분석할 수 있다.
- 쪽수는 대체 글꼴에 좌우되며 원본(한글 프로그램) 쪽수와 대조하지 않았다. 쪽수는 비용 추정용이며 파싱 정답은 법제처 조문이다.
- 폐지 규정과 이름이 같은 현행 규정은 규정명 매칭이 "모호함"으로 실패할 수 있다. 현행 우선 규칙은 넣지 않았으며, 실제 실행에서 나온 "규정 매칭 실패" 목록을 원인별로 확인한 뒤 별도 규칙 필요성을 판단한다.
- HWP 3.x와 구형 OLE 파일(.doc 등)은 H2Orestart 미지원·비대상이므로 변환과 파싱에서 빠진다. 누락 규모는 형식별 수로 보고한다.
- "개정 이력 3개 이상"은 과거 버전 3개 이상으로 해석함(현행 포함 여부는 실측 후 재검토).
- 파일명 날짜가 불규칙하면 정확 매칭이 어렵고, 매칭 실패 파일을 제외하므로 과거 버전 표본이 감소할 수 있다.
- 같은 버전이라도 법제처 구조화 조문과 ALIO 원본 사이에 편집 차이가 있을 수 있으므로 M2에서 정답 텍스트와 별표를 대조해야 한다.
