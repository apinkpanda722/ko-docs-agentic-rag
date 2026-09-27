---
name: failure-patterns
description: 이 프로젝트에서 실제로 겪은 실패 패턴과 재발 방지 규칙. /harness로 step을 설계할 때와 /review로 변경을 검토할 때 반드시 먼저 읽는다. 수집·변환·정제·파싱 코드, 외부 도구 연동, 배치 처리, manifest 기록을 다루는 작업이면 해당 항목을 step 금지사항과 테스트로 옮긴다.
---

# 실패 패턴

같은 실패를 두 번 겪지 않기 위한 기록이다. 항목마다 **강제 수단**이 있다. 강제 수단이 테스트면 그 테스트가 1차 방어선이고, 이 문서는 새 코드에 같은 패턴을 옮길 때 참고한다.

## 사용법

- **/harness (step 설계)**: 작업과 관련된 항목을 골라 그 step의 `## 금지사항` 또는 `## 작업`에 **이유와 함께** 옮겨 적는다. Codex는 이 파일을 읽지 않고 step 파일만 읽는다.
- **/review**: 변경된 코드에 아래 패턴이 있는지 확인한다. 이번 phase에서 새로 겪은 실패는 이 문서에 항목으로 추가한다.
- **승격**: 같은 패턴이 두 번째로 나오거나 기계적으로 검사할 수 있으면 테스트로 만들고 강제 수단에 테스트 이름을 적는다.

## 항목 형식

`### FP-NNN 제목` / 증상 / 원인 / 규칙 / 강제 수단 / 출처(phase·커밋)

---

## A. 산출물 검증

### FP-001 형식만 보고 성공 판정 → 조용한 품질 실패
- **증상**: HWP → PDF 변환이 종료 코드 0, PDF 시그니처 정상인데 한글이 전부 빈칸. 전 파일이 "성공"으로 기록될 뻔함.
- **원인**: 성공 판정이 파일 존재·시그니처뿐이었다.
- **규칙**: 산출물은 **내용 지표**로 판정한다. 비율이 아니라 깨졌을 때 확실히 무너지는 지표를 실측해서 고른다(깨진 PDF도 pypdf는 "제제제"처럼 한글을 뽑으므로 한글 비율은 무용하다. 서로 다른 한글 음절 수는 행동강령 샘플 실측에서 깨진 PDF 15, 정상 387로 갈렸다).
- **강제 수단**: `conversions.jsonl`에 `hangul_distinct`·`embedded_fonts` 기록(`test_ok_record_includes_embedded_fonts_and_hangul_distinct`), M1 리포트의 "`hangul_distinct=0`인 파일 수". 변환을 막는 게이트는 아직 없다. 실측에서 0인 파일이 나오면 게이트 테스트로 승격한다.
- **출처**: M1 step 5 (40898da)

### FP-002 GUI가 떠 있는 상태의 확인 ≠ headless 단독 실행
- **증상**: 처음 변환 확인 때는 한글 정상. GUI를 닫고 돌리자 한글 깨짐.
- **확인된 사실**: GUI가 떠 있던 때의 변환은 한글이 정상이었다. GUI를 닫고 headless 단독으로 실행하면 한글이 빈칸이었고, 함초롬을 설치해도 같았다. `FONTCONFIG_FILE`로 시스템·사용자 글꼴 폴더와 대체 규칙을 넘기자 정상이 됐다.
- **원인** [추정, 미검증]: headless 호출이 GUI 인스턴스로 위임되어 macOS 글꼴을 썼을 가능성이 있다. 단독 headless는 fontconfig를 쓰는데 시스템 글꼴 폴더가 설정에 없었을 가능성이 있다.
- **규칙**: 로컬 도구 실측은 **실제 파이프라인과 같은 조건**(GUI 종료, 같은 env)에서 한다. 도구 실행 환경(env·설정 파일)은 코드가 명시적으로 넘긴다.
- **강제 수단**: `infra/fontconfig/fonts.conf` + `FONTCONFIG_FILE` 전달 테스트, fonts.conf 없으면 변환 전 중단
- **출처**: M1 step 5 (40898da, 71460c8)

### FP-003 대체 규칙을 이름 하나씩 추가 → 끝없는 두더지 잡기
- **증상**: 샘플마다 대체 규칙에 없는 글꼴이 나와 히라기노(일본어) 등 임의 글꼴로 떨어짐.
- **규칙**: 열거형 매핑에는 **최종 기본값**을 둔다(fontconfig `append_last`). 명시 규칙은 기본값과 다르게 가야 하는 것만.
- **강제 수단**: fonts.conf 마지막 대체 규칙 테스트(`test_repo_fonts_conf_pins_last_resort_fallback_to_hcr_dotum`). 규칙 밖 글꼴이 남으면 `parse_targets.json`의 `unexpected_fonts`(예상 글꼴 `HCR*`·`Liberation*` 외)와 리포트의 "예상 외 글꼴이 섞인 파일 수"로 드러난다.
- **출처**: M1 (71460c8, 278951f)

## B. 입력 판정·파싱

### FP-010 컨테이너 형식을 문서 형식으로 착각
- **증상**: OLE 시그니처를 모두 HWP5로 판정 → `.doc`·`.xls`도 HWP로 기록, HWP 3.x는 UNKNOWN → 다운로드 실패로 삭제될 뻔함.
- **규칙**: 컨테이너(OLE·zip) 시그니처만으로 판정하지 않는다. 내부 식별 정보(HWP `FileHeader` 스트림, HWPX `mimetype`)까지 확인한다. 판정 불가 형식이 "실패 → 삭제"로 가지 않게 한다.
- **강제 수단**: `tests/ingest/test_filetype.py`
- **출처**: M1 step 1 이후 (8da62d7)

### FP-011 외부 라이브러리의 예외 계층을 추측
- **증상**: `except OSError`만 잡았는데 olefile 0.47은 손상 입력에 `ValueError`를 던짐 → 깨진 응답 하나로 수집 전체 중단.
- **규칙**: 외부 라이브러리가 손상 입력에서 던지는 예외는 **손상 입력을 실제로 넣어** 확인하고 회귀 테스트로 남긴다.
- **강제 수단**: 손상 OLE 회귀 테스트
- **출처**: M1 (011e20d)
- **M2 사례(속성 가정)**: Docling 어댑터가 표가 아닌 모든 요소에 `text`가 있다고 가정했다. mock에 그림 요소가 없어서(오히려 그림에 `text`를 넣어서) 테스트가 통과했지만, 실제 `make parse-docling`에서는 115건 중 41건이 `'PictureItem' object has no attribute 'text'`로 실패했다. 외부 라이브러리 어댑터는 **실제 출력 1건으로 요소 종류(클래스·라벨·속성)를 확인한 뒤** fixture에 반영한다. 없는 속성은 추측해 채우지 말고 건너뛴 사실을 기록한다(`skipped_items`). 강제 수단: `tests/parsing/test_docling_adapter.py`(`test_items_without_text_are_skipped_and_counted_per_label`, `test_other_text_less_item_kinds_are_skipped_not_filled`).
- **M2 사례(응답 필드명 추측)**: 업스테이지 응답의 API 버전을 문서 추측으로 `apiVersion`에서 읽어 모든 기록이 빈 값이었다. 실제 응답 키는 `api`("2.0")였고 테스트 fixture도 추측한 키를 써서 통과했다. API 응답 필드명은 **실제 응답(캐시) 키로 확인**한 뒤 코드와 fixture에 반영한다. 강제 수단: `test_enhanced_usage_warns_and_is_saved_in_settings_and_run_record`(`api_version == "2.0"`).

### FP-012 인코딩 가정 순서
- **증상**: UTF-8 플래그 없는 zip 멤버 이름을 곧장 cp437→cp949로 복원 → macOS에서 만든 UTF-8 zip은 깨진 이름, 파일명 속 한글 날짜 소실.
- **규칙**: 레거시 인코딩 복원은 UTF-8 → cp949 → 원래 값 순으로 시도한다.
- **강제 수단**: 플래그 없는 UTF-8·cp949 멤버 이름 테스트
- **출처**: M1 (3dcab23)

### FP-013 외부 도구의 출력 파일 이름 추측
- **증상**: `outdir / f"{src.stem}.pdf"`로 결과를 찾음 → 유니코드 정규화 등으로 이름이 다르면 성공을 실패로 기록.
- **규칙**: 입력마다 격리된 출력 폴더를 쓰고, 그 안의 결과 파일이 **정확히 1개**인지로 찾는다.
- **강제 수단**: 다른 이름으로 출력하는 가짜 runner 테스트
- **출처**: M1 (89c4fb1)

### FP-014 확장자만 다른 입력의 출력 덮어쓰기
- **증상**: `a.hwp`·`a.hwpx`가 같은 `a.pdf`로 변환되어 서로 덮어씀.
- **규칙**: 출력 이름에 원본 확장자와 내용 해시를 넣는다.
- **강제 수단**: 충돌 테스트(`test_same_stem_hwp_and_hwpx_create_distinct_pdfs_in_distinct_outdirs`)
- **출처**: M1 step 5 (839b5ab), 사용자 로컬 실측

### FP-015 외부 데이터의 이름·제목 불일치
- **증상**: 세 가지 형태로 나타났다. ALIO 제목에 기관의 옛 이름(한국정보화진흥원)이 남아 있었다. ALIO 목록 제목만 갱신되지 않았다(인권경영 "운영지침"이지만 첨부 파일명·법제처는 "이행지침"). 법제처에 같은 규정이 두 `rule_id`로 중복 등록되어 있었다. 모두 규정명 매칭 실패나 모호함으로 해당 규정의 파일이 통째로 빠졌다.
- **규칙**: 이름 매칭이 실패하면 다른 필드(첨부 파일명·날짜)로 교차 확인한다. 그 뒤 정규화 규칙으로 처리하거나, 근거를 남긴 명시 별칭으로 처리한다. 별칭은 사례별로만 추가하고 일반화하지 않는다.
- **강제 수단**: `tests/ingest/test_matching.py`(옛 이름 접두어, 별칭 1건, 현행 우선)
- **출처**: M1 실측 (89e872d, 5f83c99)
- **M2 사례(FP-020 연계)**: pypdf 실측에서 법제처에만 있는 조 17개 중 15개가 원본의 '의' 누락 오타(`제6조4(…)`, 법제처는 `제6조의4`), 2개는 원본≠법제처 내용 차이였다. 분할기는 줄 첫머리 + 괄호 제목일 때만 키를 교정 인식하고 원문 텍스트는 바꾸지 않으며 `notation_variants`로 집계한다. 모든 파서가 공통으로 놓친 키는 `answer_differences`로 따로 보고한다(`tests/parsing/test_articles.py`, `tests/eval/test_parsing_score.py`).

### FP-016 합성 예시만으로 정한 경계 규칙이 실제 표기 변형을 놓침
- **증상**: 조문 분할기가 합성 테스트는 모두 통과했지만, 실제 법제처 267개 버전에 돌리자 5개 버전에서 조를 놓쳤다(구조 완전 일치 262/267). 원인은 두 가지였다. 꺾쇠괄호 삭제 표기(`제5조 <삭제 2017. 5. 25.>`, 법제처 12건)를 맨 `삭제`만 허용한 정규식이 인식하지 못했다. 조 제목 상한 40자가 실측 최대 51자(1건)보다 짧았다. 이 누락은 정답과 파서 출력 양쪽에 똑같이 걸려 모든 파서의 점수를 같은 이유로 깎는다.
- **규칙**: 정규식 경계 규칙은 합성 예시로 정한 뒤 **실제 정답 데이터 전체에 돌려 상한을 재고**, 놓친 사례를 표기 변형별로 센 다음에 확정한다. 길이 상한은 실측 최대값에 여유를 두어 정한다. 분할기 상한(정답을 이어 붙여 분할했을 때 복원율)은 파서 점수와 같은 표에 보고한다.
- **강제 수단**: `tests/parsing/test_articles.py`(꺾쇠·대괄호·공백 없는 삭제 표기, 80자 제목), step 6의 `splitter-ceiling` 행
- **출처**: M2 step 5 이후 실측 (분할기 상한 262/267 → 267/267)

## C. 정제·정답 데이터

### FP-020 정제가 정답 텍스트를 조용히 바꿈
- **증상**: 비밀값 제거용 `sanitize_url`을 조문 포함 모든 문자열에 적용 → `?…&oc=` 모양 조각이 잘리고 URL scheme이 소문자로 바뀔 수 있음. 이 텍스트는 파싱 정답이다.
- **규칙**: 변환은 필요한 필드에만 적용한다. 정답·원문 필드는 **정제 전후 동일** 테스트를 둔다.
- **강제 수단**: `?`·`&`·`oc=`·`Note:`가 섞인 조문 동일성 테스트
- **출처**: M1 step 2 이후 (011e20d)

### FP-021 정답이 없거나 의심스러운 대상·키가 분모에 들어감
- **증상**: 두 가지로 나타났다. 법제처 본문이 이미지 한 줄인 대상(조 머리 0개)은 모든 파서를 조 구조 불일치·텍스트 0점으로 깎았고, 분할기 상한은 "0개=0개 일치"로 부풀렸다. 원본≠법제처인 조 키(원본 누락·번호 체계 차이)는 모든 파서의 재현율을 같은 이유로 깎아 파서 차이처럼 보였다.
- **규칙**: 정답이 없는 대상은 별도 상태(`no_law_text`)로 표시해 모든 지표 분모와 분할기 상한 분모에서 빼고, 제외 목록(id·사유)을 manifest에 남긴다. 파서 여럿이 공통으로 놓친 정답 키는 정답 차이로 따로 집계하고 원래 값과 보정 값을 함께 보고한다. 정답(golden·법제처 텍스트)은 고치지 않는다.
- **강제 수단**: `tests/eval/test_parsing_score.py`(`test_law_without_article_text_is_excluded_from_parser_and_ceiling_denominators`, `test_keys_missed_by_every_ok_parser_are_reported_as_answer_differences`, `test_answer_differences_need_two_scored_comparison_parsers`)
- **출처**: M2 실측 (383cc1d, bf37332)

### FP-022 조합 생성한 가상 이름이 실존 공인과 겹칠 수 있음
- 음절 조합으로 만든 가상 인물 이름이 전직 대통령 이름과 같았다(M3 step 1) → 열린 목록(실존 인물 명단) 대조가 아니라 **이름 풀 설계(요즘식·순우리말 이름) + 사람 검토(`make seed-names` 후 `--rename PERSON_ID`)**로 막고, 인물은 이름이 아니라 `person_id`로 참조한다. 강제 수단: `data/seed/README.md` 인물 명단 검토 절차, `tests/synth/test_bootstrap_people.py`.

## D. 배치 수집·기록

### FP-030 항목 하나의 실패가 배치 전체를 멈춤
- **증상**: ALIO 상세 조회·zip 해제 실패가 예외로 올라가 뒤 규정을 못 받음.
- **규칙**: 항목 단위 실패는 실패 기록(jsonl)에 남기고 계속한다. 진행이 불가능한 단계(목록 조회)만 전체 중단.
- **강제 수단**: "1건 실패 후 계속" 테스트
- **출처**: M1 step 3 이후 (3dcab23)

### FP-031 필터 결과 0건이 성공으로 끝남
- **증상**: `소관부처명` 완전 일치 필터가 실제 응답과 조금만 달라도 0건 수집 후 종료 코드 0.
- **규칙**: 반드시 1건 이상이어야 하는 결과가 0건이면 실패로 멈추고 원인 후보를 알린다.
- **강제 수단**: 현행 0개 테스트
- **출처**: M1 (011e20d)

### FP-032 append-only 기록의 중복 줄을 그대로 읽음
- **증상**: 재수집 시 같은 키 줄이 추가되어 매칭 결과 중복, 자기 자신과 "중복 매칭", 집계 부풀림.
- **규칙**: append-only manifest는 `latest_records(key)`로 읽는다. 키를 명시한다.
- **강제 수단**: 중복 입력 테스트
- **출처**: M1 step 4 이후 (098dd3f)

### FP-033 멱등성 키와 출력 키 불일치
- **증상**: 변환 기록을 원본 해시로 찾는데 출력 경로는 파일명 기준 → 같은 내용 파일 둘이 서로 기록을 덮어써 매번 재변환.
- **규칙**: 멱등성 판단 키는 **출력을 결정하는 키**와 같게 한다.
- **강제 수단**: 같은 해시 두 레코드 재실행 테스트
- **출처**: M1 (89c4fb1)

### FP-034 장시간 배치가 진행 상황을 출력하지 않음
- **증상**: `collect-law`가 수 분간 아무것도 출력하지 않아 멈춘 것으로 오인했다.
- **규칙**: 1분 넘게 걸릴 수 있는 명령은 N건마다 진행 줄을 `flush=True`로 출력한다(make·uv를 거쳐도 즉시 보이게). 진행 줄에 URL·인증값을 넣지 않는다.
- **강제 수단**: 수집기·변환 진행 출력 테스트(`test_collect_prints_progress_*`, `test_run_prints_progress_*`)
- **출처**: M1 실측 (c177148, da58f2e)

### FP-035 전송 후 실패를 재시도해 이중 과금
- **증상**: 업스테이지 요청의 ReadTimeout·WriteTimeout·RemoteProtocolError 같은 전송 후 실패를 재시도하면 서버가 이미 처리·과금했을 수 있다. 기본 타임아웃 300초는 긴 문서(대상 최대 85쪽)의 동기 파싱에서 불필요한 실패를 냈다.
- **규칙**: 과금 API는 요청 전송 전 실패(ConnectError·ConnectTimeout)와 429·5xx만 재시도한다. 전송 후 실패는 재전송하지 않고 `billing_uncertain=true`로 그 문서만 실패 기록한다. 인증·결제·한도 오류(401·402·403, 한도 초과 4xx, 지속 429)는 실행 전체를 멈춘다. 타임아웃은 대상 최대 쪽수로 정한다.
- **강제 수단**: `tests/parsing/test_upstage_adapter.py`(`test_post_send_transport_error_is_not_retried_and_marks_billing_uncertain`, `test_read_timeout_records_failed_billing_uncertain_without_ledger`, `test_fatal_response_raises_fatal_error`, `test_persistent_rate_limit_after_retries_is_fatal`, `test_default_timeout_is_600_seconds_for_long_documents`)
- **출처**: M2 step 4 이후 (9b8f866, acb5a25)

### FP-036 1회성 초기화 시간이 문서별 처리 시간에 섞임
- **증상**: Docling 레이아웃·표 모델 로딩이 첫 문서의 `elapsed_s`에 들어가 문서별·쪽당 처리 시간이 부풀었다. 실행 장치(mps·cuda·cpu)가 기록되지 않아 시간 비교의 조건을 알 수 없었다.
- **규칙**: 모델 로딩 같은 1회성 초기화는 어댑터 `prepare()`로 분리해 `init_s`에 따로 기록하고 문서별 시간에서 뺀다. 초기화 실패는 어떤 입력도 성공할 수 없으므로 실행 전체를 멈춘다. 실행 장치는 settings에 기록한다(FP-044).
- **강제 수단**: `tests/parsing/test_run.py`(`test_run_prepares_once_and_records_init_time_separately`, `test_run_stops_when_adapter_initialization_fails`), `tests/parsing/test_docling_adapter.py`(`test_prepare_loads_converter_once_and_parse_times_only_conversion`)
- **출처**: M2 step 3 이후 (42777a9)
- **M2 사례(집계 분모·분자의 대상 집합 불일치)**: `parse-docling` 요약의 sec/page가 1차에는 실패 문서 시간을 분자에 넣어 0.884, 2차에는 건너뛴 문서 쪽수를 분모에 넣어 0.223이었다(문서별 elapsed_s 합 기준 실측 0.384). 결과 보고서도 쪽당 시간에 init_s를 더했다. 비율 지표는 **분자와 분모를 같은 대상 집합**(이번 실행에서 파싱한 ok 문서)에서 구하고, 초기화 시간은 별도 열로 둔다. 강제 수단: `test_seconds_per_page_uses_only_documents_parsed_ok_in_this_run`, `test_cost_table_excludes_model_loading_from_seconds_per_page`.

### FP-037 과금 원장을 공급자 청구와 대조하지 않아 과소 기록을 모름
- **증상**: 업스테이지 원장은 응답 `usage.pages` 합 1887쪽(115건)인데 콘솔 청구는 1947쪽(+60쪽, +3.2%)이었다. 응답 쪽수·캐시·원장 건수가 모두 맞아 코드만으로는 차이를 알 수 없었다. 콘솔 CSV는 일 합계 한 줄뿐이라 요청 단위 대조도 불가능했고, 원인(미기록 5xx 재시도 과금 또는 청구 쪽수 계산 차이 추정)은 확인하지 못했다.
- **규칙**: 과금 API의 첫 실행 뒤 공급자 콘솔 청구와 원장을 대조하고, 차이는 예산 검사 여유(`billing_margin`)로 반영한다. 원장은 응답 값 그대로 두고 여유는 사전 검사·dry-run에만 쓴다. 요청마다 HTTP 시도별 상태를 남겨(`attempts`) 다음 대조에서 재시도 과금을 가려낼 수 있게 한다. 과금 실행 절차에 실행 전후 콘솔 Usage 기록을 넣는다.
- **강제 수단**: `tests/parsing/test_upstage_adapter.py`(`test_budget_check_uses_billing_margin_but_ledger_keeps_usage_pages`, `test_client_records_status_and_time_of_every_attempt`, `test_failed_request_carries_its_attempts_into_the_run_record`), 결과 문서 틀 2절 9번·7절
- **출처**: M2 업스테이지 실측 대조
- **2차 대조(M2 직접 입력 14건)**: 원장 276쪽 = 콘솔 증가분 276쪽(1,947→2,223)으로 정확히 일치했고, `attempts` 기록상 전 요청이 1회 시도·HTTP 200이었다. 1차 +60쪽은 시도 기록 도입 전의 일회성 차이로 보되, 원인이 확정되지 않아 `billing_margin`은 유지한다.

## E. 프로세스

### FP-040 지시에 없는 네트워크 요청·다운로드
- **증상**: 실측용 샘플을 승인 없이 ALIO에서 받음.
- **규칙**: 지시에 없는 네트워크 요청·다운로드·설치는 **실행 전에 묻는다**. 실제 API 호출 명령은 사용자가 실행한다.
- **강제 수단**: step 금지사항(“네트워크 요청을 하지 마라” + 이유)
- **출처**: M1 step 1 이후

### FP-041 세션 간 전달한 수정 요청이 반영되지 않음
- **증상**: 검토 세션(Obsidian)이 step 진행 중에 준비한 수정 요청(filetype `ValueError` 처리)이 구현 세션에 전달되지 않았다. 다음 검토에서야 발견됐다.
- **규칙**: 여러 항목을 요청받으면 구현 쪽은 **항목별 반영 여부와 커밋**을 보고한다. 검토 쪽은 다음 검토에서 **이전 요청 항목을 커밋과 대조**한다.
- **강제 수단**: /review 체크리스트 8번(요청 반영)
- **관련**: FP-011 (`ValueError` 버그의 기술적 원인)
- **출처**: M1 step 2 이후 (011e20d에서 반영)

### FP-042 step 실행 중 작업 트리 변경
- **증상**: `.env.example` 수동 수정·미추적 파일이 `execute.py`의 깨끗한 작업 트리 검사를 막음. 수집 명령은 커밋 대상 manifest를 쓴다.
- **규칙**: phase 실행 중에는 작업 트리를 건드리지 않는다. 미리보기 수집은 `--root`로 저장소 밖에 한다.
- **강제 수단**: `execute.py` 작업 트리 검사, `.env.example` 동기화 테스트
- **출처**: M1 step 0 이후

### FP-043 모호한 수량 기준
- **증상**: "개정 이력 3개 이상"을 과거 버전 3개로 해석할지 전체 버전 3개로 해석할지 갈림.
- **규칙**: step 설계에서 수량 기준은 **무엇을 세는지**(현행 포함 여부 등)까지 적는다.
- **강제 수단**: /harness step 설계 시 확인
- **출처**: M1 step 6

### FP-044 환경에 좌우되는 결과에 측정 환경을 기록하지 않음
- **증상**: M1 결과 문서에 날짜·커밋·도구 버전·글꼴 환경이 없어 /review에서 지적됨. 쪽수·글꼴·줄바꿈은 LibreOffice·H2Orestart 버전과 fonts.conf에 따라 달라진다.
- **규칙**: 결과 문서는 EVAL.md "결과 보고" 규칙(파일명 날짜, 커밋 해시, 도구·모델 버전, 고정 조건, 측정 환경)을 따른다. 도구·설정 파일 버전은 해시로 적는다.
- **강제 수단**: /review 체크리스트 6번
- **출처**: M1 /review (2b6963f)

## F. 이후 phase에 미리 적용할 것

### FP-050 식별자가 분석기에 쪼개짐 (M5)
- **증상(사전 확인)**: standard 분석기가 `PAY-1234`를 `pay`/`1234`로 쪼갬.
- **규칙**: 식별자는 적재 시 정규식으로 추출해 `keyword` 필드에 넣고 `term`으로 조회한다.
- **강제 수단**: M5에서 테스트로 승격
- **출처**: M0 OpenSearch 확인
