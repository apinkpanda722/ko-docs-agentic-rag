# M8 공개 전환 점검

<!-- BEGIN generated -->
## 결정론 점검 집계

발견 건수는 공개 판정이 아니다. 패턴은 참고 위치이며 매치 값은 기록하지 않는다. 원본 전체는 `data/manifest/m8_audit.json`에 있다.

### 실행 정보

| 항목 | 값 |
| --- | --- |
| executed_at | 2026-10-02T14:53:05.665034+00:00 |
| head_commit | e9545e55e494599d9120b47c00fb8cb8de1cc9a7 |

### 패턴별 합계

| 패턴 | 작업 트리 | 이력 |
| --- | --- | --- |
| absolute_path | 9 | 15 |
| assignment | 35 | 121 |
| law_oc | 16 | 47 |
| phone | 10 | 17 |

### 작업 트리 발견

| 경로 | 패턴 | 건수 |
| --- | --- | --- |
| .claude/skills/failure-patterns/SKILL.md | law_oc | 1 |
| data/manifest/m7_probe.json | absolute_path | 3 |
| data/manifest/m7_security_set.json | phone | 1 |
| docs/results/2026-10-02-m7-answer-eval-appendix.md | absolute_path | 3 |
| phases/m0-setup/step2.md | assignment | 1 |
| phases/m1-data-collection/step1.md | law_oc | 1 |
| phases/m1-data-collection/step2.md | law_oc | 2 |
| phases/m1-data-collection/step2.md | phone | 2 |
| phases/m7-answer-eval/step3.md | law_oc | 1 |
| phases/m8-wrap-up/step1.md | absolute_path | 1 |
| src/kodocs/eval/m8_audit.py | absolute_path | 1 |
| src/kodocs/eval/query_draft.py | assignment | 2 |
| src/kodocs/llm/gemini_chat.py | assignment | 1 |
| src/kodocs/llm/openai_chat.py | assignment | 1 |
| src/kodocs/mcp_server/server.py | assignment | 1 |
| src/kodocs/mcp_server/tokens.py | assignment | 1 |
| src/kodocs/parsing/run.py | assignment | 1 |
| src/kodocs/synth/generate.py | assignment | 1 |
| tests/agent/test_trace.py | assignment | 1 |
| tests/eval/test_m6_report.py | assignment | 2 |
| tests/eval/test_m7_grading.py | law_oc | 1 |
| tests/eval/test_m7_latency.py | assignment | 1 |
| tests/eval/test_m7_security.py | assignment | 1 |
| tests/eval/test_m8_stream_repro.py | assignment | 1 |
| tests/ingest/fixtures/law_body_full.json | law_oc | 2 |
| tests/ingest/fixtures/law_body_full.json | phone | 1 |
| tests/ingest/fixtures/law_body_scalar.json | phone | 1 |
| tests/ingest/fixtures/law_search_current_page1.json | law_oc | 1 |
| tests/ingest/test_http.py | law_oc | 1 |
| tests/ingest/test_law_api.py | law_oc | 3 |
| tests/ingest/test_law_api.py | phone | 4 |
| tests/ingest/test_manifest.py | law_oc | 2 |
| tests/llm/test_anthropic_batch.py | assignment | 1 |
| tests/llm/test_gemini_chat.py | assignment | 2 |
| tests/llm/test_openai_chat.py | assignment | 1 |
| tests/mcp_server/conftest.py | assignment | 2 |
| tests/parsing/test_upstage_adapter.py | assignment | 1 |
| tests/parsing/test_upstage_direct.py | assignment | 1 |
| tests/test_config.py | assignment | 10 |
| tests/test_m8_audit.py | absolute_path | 1 |
| tests/test_m8_audit.py | assignment | 1 |
| tests/test_m8_audit.py | law_oc | 1 |
| tests/test_m8_audit.py | phone | 1 |
| tests/ui/test_users_service.py | assignment | 1 |

### 이력 발견(경로 단위)

| 경로 | 해당 blob 수 | 패턴별 최대 건수 |
| --- | --- | --- |
| .claude/skills/failure-patterns/SKILL.md | 20 | law_oc: 1 |
| data/manifest/m7_probe.json | 1 | absolute_path: 3 |
| data/manifest/m7_security_set.json | 1 | phone: 1 |
| docs/results/2026-10-02-m7-answer-eval-appendix.md | 1 | absolute_path: 3 |
| docs/results/2026-10-02-m7-answer-eval.md | 1 | absolute_path: 3 |
| phases/m0-setup/step2.md | 1 | assignment: 1 |
| phases/m1-data-collection/step1.md | 1 | law_oc: 1 |
| phases/m1-data-collection/step2.md | 1 | law_oc: 2, phone: 2 |
| phases/m7-answer-eval/step3.md | 3 | law_oc: 1 |
| phases/m8-wrap-up/step1.md | 2 | absolute_path: 1 |
| src/kodocs/eval/m8_audit.py | 2 | absolute_path: 1 |
| src/kodocs/eval/query_draft.py | 9 | assignment: 2 |
| src/kodocs/llm/gemini_chat.py | 3 | assignment: 1 |
| src/kodocs/llm/openai_chat.py | 7 | assignment: 1 |
| src/kodocs/mcp_server/server.py | 1 | assignment: 1 |
| src/kodocs/mcp_server/tokens.py | 1 | assignment: 1 |
| src/kodocs/parsing/run.py | 8 | assignment: 1 |
| src/kodocs/synth/generate.py | 9 | assignment: 1 |
| tests/agent/test_trace.py | 2 | assignment: 1 |
| tests/eval/test_m6_report.py | 6 | assignment: 2 |
| tests/eval/test_m7_grading.py | 5 | law_oc: 1 |
| tests/eval/test_m7_latency.py | 1 | assignment: 1 |
| tests/eval/test_m7_security.py | 4 | assignment: 1 |
| tests/eval/test_m8_stream_repro.py | 3 | assignment: 1 |
| tests/ingest/fixtures/law_body_full.json | 1 | law_oc: 2, phone: 1 |
| tests/ingest/fixtures/law_body_scalar.json | 1 | phone: 1 |
| tests/ingest/fixtures/law_search_current_page1.json | 1 | law_oc: 1 |
| tests/ingest/test_http.py | 1 | law_oc: 1 |
| tests/ingest/test_law_api.py | 3 | law_oc: 3, phone: 4 |
| tests/ingest/test_manifest.py | 2 | law_oc: 2 |
| tests/llm/test_anthropic_batch.py | 1 | assignment: 1 |
| tests/llm/test_gemini_chat.py | 3 | assignment: 2 |
| tests/llm/test_openai_chat.py | 7 | assignment: 1 |
| tests/mcp_server/conftest.py | 1 | assignment: 2 |
| tests/parsing/test_upstage_adapter.py | 5 | assignment: 1 |
| tests/parsing/test_upstage_direct.py | 3 | assignment: 1 |
| tests/test_config.py | 3 | assignment: 10 |
| tests/test_m8_audit.py | 2 | absolute_path: 1, assignment: 1, law_oc: 1, phone: 1 |
| tests/ui/test_users_service.py | 1 | assignment: 1 |

### 슁글 겹침 상위 30개 파일

| 경로 | 원 겹침 슁글 수 | 최대 길이 | 한글 슁글 수 | 한글 최대 길이 |
| --- | --- | --- | --- | --- |
| scripts/test_execute.py | 1196 | 75 | 0 | 0 |
| tests/eval/test_golden_review.py | 92 | 75 | 0 | 0 |
| tests/eval/test_golden_specs.py | 552 | 75 | 0 | 0 |
| tests/eval/test_query_draft.py | 92 | 75 | 0 | 0 |
| docs/results/2026-09-30-m5-retrieval.md | 64 | 51 | 0 | 0 |
| docs/results/2026-10-01-m6-agent-mcp.md | 80 | 51 | 0 | 0 |
| data/manifest/law_api.jsonl | 113 | 48 | 113 | 48 |
| docs/results/2026-09-27-m2-parsing.md | 39 | 47 | 0 | 0 |
| phases/m4-golden-set/step0.md | 16 | 45 | 0 | 0 |
| docs/results/2026-09-27-m3-synthetic.md | 16 | 41 | 0 | 0 |
| docs/results/2026-09-28-m4-golden-set.md | 16 | 39 | 0 | 0 |
| docs/results/2026-10-02-m7-answer-eval-appendix.md | 30 | 39 | 0 | 0 |
| docs/results/2026-10-02-m7-answer-eval.md | 30 | 39 | 0 | 0 |
| tests/eval/test_text_normalize.py | 9 | 38 | 0 | 0 |
| tests/eval/test_parsing_score.py | 4 | 33 | 0 | 0 |
| src/kodocs/eval/templates/m3_synthetic.md | 4 | 32 | 0 | 0 |
| tests/parsing/test_articles.py | 3 | 32 | 3 | 32 |
| data/manifest/table_golden.json | 1 | 30 | 0 | 0 |

NFC·공백 제거 후 원문 30자 창으로 해시 대조. 한글 슁글은 해시 전 이 창의 U+AC00–U+D7A3 음절이 20자 이상. 원/한글 건수는 중복 창 포함 위치 수; 최대 길이는 연속 일치 창 수+29(없으면 0). 한글 최대 길이는 필터도 연속 통과한 창 기준.
파일별 열은 작업 트리·이력 관측값의 각각 최대값이다.

### 원문 대조 상태

| 상태 | 입력 위치·파일 수 |
| --- | --- |
| available | data/interim/law: 267; data/interim/parsed: 359 |

### 이메일 도메인

| 도메인 | 건수 | 구분 | 역할 |
| --- | --- | --- | --- |
| gmail.com | 355 | personal_service | author |
| liveuou.kr | 21 | other | author |
| mediazen.co.kr | 5 | other | author |
| github.com | 21 | other | committer |
| gmail.com | 355 | personal_service | committer |
| mediazen.co.kr | 5 | other | committer |

### 커밋 해시 인벤토리

| 항목 | 건수 |
| --- | --- |
| pairs | 31 |
| distinct_hashes | 17 |
| files | 24 |
| existing | 17 |
| missing | 0 |
| rev_parse_files | 10 |
| rev_parse_calls | 15 |

서로 다른 (파일, 해시) 쌍: 커밋|commit JSON 키의 값 또는 커밋|commit|code_commit이 있는 줄의 7~40자 소문자 16진 토큰. SHA-256은 제외.

### rev-parse 호출 위치

| 파일 | 줄 |
| --- | --- |
| src/kodocs/eval/m4_report.py | 408 |
| src/kodocs/eval/m5_dev.py | 507 |
| src/kodocs/eval/m5_report.py | 1147 |
| src/kodocs/eval/m5_test.py | 841 |
| src/kodocs/eval/m6_dev.py | 885 |
| src/kodocs/eval/m8_audit.py | 279 |
| src/kodocs/eval/m8_audit.py | 471 |
| src/kodocs/eval/m8_audit.py | 472 |
| src/kodocs/eval/m8_audit.py | 647 |
| src/kodocs/eval/parsing_report.py | 127 |
| src/kodocs/eval/parsing_report.py | 912 |
| src/kodocs/eval/parsing_score.py | 1043 |
| src/kodocs/eval/synth_report.py | 160 |
| src/kodocs/eval/synth_report.py | 969 |
| src/kodocs/retrieval/index_build.py | 1034 |

### 저장소 위생

| 항목 | 값 |
| --- | --- |
| .env.example | True |
| LICENSE | False |
| README.md | True |
| env_in_history | False |

### 민감 경로 인벤토리

| 구분 | 종류 | 건수 |
| --- | --- | --- |

### 스캔·제외 건수

| 구분 | 항목 | 건수 |
| --- | --- | --- |
| worktree | scanned_files | 1326 |
| worktree | scanned_blobs | 0 |
| worktree | oversized | 1 |
| worktree | undecodable | 0 |
| worktree | excluded_sensitive_worktree | 0 |
| history | scanned_files | 0 |
| history | scanned_blobs | 2393 |
| history | oversized | 8 |
| history | undecodable | 0 |
| history | excluded_sensitive_worktree | 0 |

### ref 개수

| 종류 | 건수 |
| --- | --- |
| heads | 20 |
| remotes | 20 |

### GitHub 점검

| 항목 | 값 |
| --- | --- |
| status | 미실행: 사용자 실행 필요 |
<!-- END generated -->

## 사용자 결정 (사용자 보존 영역)

- [ ] 공개 여부(사용자 결정). 결정: 
  - 근거: 생성 영역 전체, [위협 모델](../THREAT_MODEL.md).
- [ ] 이력 처리. 결정: 
  - **기본안: 이력 유지.** 결과 문서·manifest의 커밋 참조(m5_config.code_commit, M7 측정 코드 커밋, rev-parse 기록 코드)가 이력 재작성 후 없는 커밋을 가리키고, 결과를 보기 전에 규칙을 커밋했다는 시간순 근거가 깨진다. 새 저장소(스쿼시)도 같은 이유로 제외한다. 근거: [ADR-026](../ADR.md), 생성 영역 commit_inventory.
  - 이력 정리 대안(`filter-repo --mailmap` + old→new SHA 대응표 + 문서 내 해시 갱신)의 비용: 회사 메일을 반드시 지워야 하는 경우만 고려. 대상 파일 수·해시 수·rev-parse 위치는 생성 영역 commit_inventory의 files·pairs·distinct_hashes·rev_parse_files·rev_parse_calls 참조. 실행 절차나 스크립트는 만들지 않는다.
- [ ] 작성자 이메일 확인. 결정: 
  - 사용자 설명: liveuou.kr 21건은 GitHub 웹 머지 커밋(계정 이메일), 회사 메일 도메인은 5건. 스캐너의 생성 영역 email_domains와 대조: 
  - [ ] 앞으로의 커밋 이메일(noreply)을 사용자가 GitHub에서 직접 설정.
- [ ] LICENSE. 결정: 
  - 코드의 기본 후보 MIT. 범위: "합성 데이터·문서는 같은 라이선스, 실제 규정 원문은 저장소에 없음". 공개 직전 사용자 최종 결정. 근거: [ADR-002](../ADR.md), 생성 영역 hygiene·paths·shingles.
- [ ] 슁글 겹침 검토. 결정: 
  - 생성 영역 worktree·history findings의 건수·최대 길이가 큰 파일을 직접 검토한다. 법령 상투구 오탐 가능성이 있어 스캐너는 판정하지 않는다. 원문 없으면 건너뜀이며 통과가 아니다.
- [ ] GitHub 이슈·PR·댓글 점검 결과(`make m8-audit-gh`) 확인. 결정: 
  - 근거: 생성 영역 github 및 data/manifest/m8_audit_gh.json. 공개하면 refs/pull/*도 함께 보인다는 점 확인(로컬 refs에는 없을 수 있음).
- [ ] 공개 직전 `make m8-audit` 재실행. 결정: 
  - 이후 커밋도 점검 대상에 들어간다. 근거: 생성 영역 executed_at·head_commit. manifest와 생성 영역은 재실행마다 덮어쓴다.
