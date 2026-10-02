.PHONY: m8-stream-repro m8-stream-repro-dry readme ui adr-index m8-wrap-report m8-parse-error verify up down logs test-integration collect-law collect-alio match-versions convert-hwp parse-targets parse-pypdf docling-models parse-docling parse-upstage-dry parse-upstage parse-upstage-direct-dry parse-upstage-direct parse-score table-drafts table-golden table-score parse-report reg-index seed-names build-db plan-docs generate-dry generate-pilot generate-pilot-rerun generate synth-validate synth-metrics synth-report review-sample review-score query-specs queries-dry queries queries-batch-dry queries-batch query-validate lexical-report golden-real-candidates golden-real-check golden-split-preview golden-packets golden-promote m4-report m5-annex-map m5-chunking m5-eval-inputs embed-models m5-index-dry m5-index m5-dev m5-freeze m5-test m5-report m6-temporal m6-inputs m6-dry m6-dev m6-freeze m6-test m6-tokens m6-serve m6-report m7-probe m7-cards m7-human-register m7-pilot m7-freeze m7-grade-dry m7-grade m7-analyze m7-evidence-check m7-security-set m7-security-dry m7-security m7-leak-review-register m7-security-rejudge m7-latency-dry m7-latency m7-failures m7-report m8-audit m8-audit-gh

# 하네스의 단일 검증 진입점. Stop hook, scripts/execute.py, step AC가 모두 이 타깃을 호출한다.
verify:
	uv run ruff check .
	uv run ruff format --check .
	uv run --group ui pytest -q

ui:
	uv run --group ui streamlit run src/kodocs/ui/app.py

up:
	docker compose -f infra/docker-compose.yml up -d --build

down:
	docker compose -f infra/docker-compose.yml down

logs:
	docker compose -f infra/docker-compose.yml logs -f opensearch

test-integration:
	uv run pytest -q -m integration

collect-law:
	uv run python -m kodocs.ingest.law_api

collect-alio:
	uv run python -m kodocs.ingest.alio

match-versions:
	uv run python -m kodocs.ingest.matching

convert-hwp:
	uv run python -m kodocs.ingest.convert

parse-targets:
	uv run python -m kodocs.ingest.parse_targets

parse-pypdf:
	uv run python -m kodocs.parsing.run --parser pypdf

docling-models:
	uv run --group docling docling-tools models download

parse-docling:
	uv run --group docling python -m kodocs.parsing.run --parser docling

parse-upstage-dry:
	uv run python -m kodocs.parsing.run --parser upstage --dry-run

parse-upstage:
	uv run python -m kodocs.parsing.run --parser upstage

parse-upstage-direct-dry:
	uv run python -m kodocs.parsing.run --parser upstage-direct --dry-run

parse-upstage-direct:
	uv run python -m kodocs.parsing.run --parser upstage-direct

parse-score:
	uv run python -m kodocs.eval.parsing_score

table-drafts:
	uv run python -m kodocs.eval.table_drafts

table-golden:
	uv run python -m kodocs.eval.table_golden

table-score:
	uv run python -m kodocs.eval.table_score

parse-report:
	uv run python -m kodocs.eval.parsing_report

reg-index:
	uv run python -m kodocs.synth.regulations

seed-names:
	uv run python -m kodocs.synth.bootstrap_people --list

build-db:
	uv run python -m kodocs.synth.db

plan-docs:
	uv run python -m kodocs.synth.plan

# 템플릿이 바뀐 유형의 통과본까지 다시 만들 때: make generate REGENERATE_TYPES=incident
REGENERATE_FLAGS = $(if $(REGENERATE_TYPES),--regenerate-types $(REGENERATE_TYPES))

generate-dry:
	uv run python -m kodocs.synth.generate --dry-run $(REGENERATE_FLAGS)

generate-pilot:
	uv run python -m kodocs.synth.generate --limit 10

generate-pilot-rerun:
	uv run python -m kodocs.synth.generate --only-attempted

generate:
	uv run python -m kodocs.synth.generate --then-validate $(REGENERATE_FLAGS)

synth-validate:
	uv run python -m kodocs.synth.validate

synth-metrics:
	uv run python -m kodocs.eval.synth_metrics

synth-report:
	uv run python -m kodocs.eval.synth_report

review-sample:
	uv run python -m kodocs.eval.synth_review sample

review-score:
	uv run python -m kodocs.eval.synth_review score
	uv run python -m kodocs.eval.synth_report

query-specs:
	uv run python -m kodocs.eval.golden_specs

queries-dry:
	uv run python -m kodocs.eval.query_draft --dry-run

queries:
	uv run python -m kodocs.eval.query_draft

queries-batch-dry:
	uv run python -m kodocs.eval.query_draft --batch --dry-run

queries-batch:
	uv run python -m kodocs.eval.query_draft --batch

query-validate:
	uv run python -m kodocs.eval.query_draft --validate-only

lexical-report:
	uv run python -m kodocs.eval.lexical report

golden-real-candidates:
	uv run python -m kodocs.eval.golden_real candidates

golden-real-check:
	uv run python -m kodocs.eval.golden_real check

golden-split-preview:
	uv run python -m kodocs.eval.golden_split preview

golden-packets:
	uv run python -m kodocs.eval.golden_review packets --attention-checks 3

golden-promote:
	uv run python -m kodocs.eval.golden_review promote && uv run python -m kodocs.eval.m4_report

m4-report:
	uv run python -m kodocs.eval.m4_report

m5-annex-map:
	uv run python -m kodocs.parsing.annex_mapping

m5-chunking:
	uv run python -m kodocs.chunking.corpus --check
	uv run python -m kodocs.chunking.chunkers

m5-eval-inputs:
	uv run python -m kodocs.eval.m5_inputs

# 사용자가 승인 후 pyproject.toml에 embed 그룹을 추가해야 한다. 이 타깃은 약 2.3GB 모델만 내려받는다.
embed-models:
	uv run --group embed python -c "from pathlib import Path; from huggingface_hub import snapshot_download; from kodocs.retrieval.embedding import BGE_M3_ALLOW_PATTERNS, BGE_M3_IGNORE_PATTERNS, BGE_M3_MODEL_ID, BGE_M3_REVISION; p = Path(snapshot_download(BGE_M3_MODEL_ID, revision=BGE_M3_REVISION, allow_patterns=list(BGE_M3_ALLOW_PATTERNS), ignore_patterns=list(BGE_M3_IGNORE_PATTERNS))); print(f'path={p}'); print(f'revision={p.name}')"

m5-index-dry:
	uv run --group embed python -m kodocs.retrieval.index_build --all --dry-run

m5-index:
	uv run --group embed python -m kodocs.retrieval.index_build --all

m5-dev:
	uv run --group embed python -m kodocs.eval.m5_dev

m5-freeze:
	uv run python -m kodocs.eval.m5_dev --freeze

M5_RERUN_FLAGS = $(if $(RERUN_REASON),--rerun-reason "$(RERUN_REASON)")

m5-test:
	uv run --group embed python -m kodocs.eval.m5_test $(M5_RERUN_FLAGS)

m5-report:
	uv run python -m kodocs.eval.m5_report

m6-temporal:
	uv run python -m kodocs.eval.m6_temporal

m6-inputs:
	uv run python -m kodocs.eval.m6_inputs

m6-dry:
	uv run python -m kodocs.eval.m6_dev --dry-run

M6_LIMIT_FLAGS = $(if $(LIMIT),--limit $(LIMIT))

m6-dev:
	uv run --group embed python -m kodocs.eval.m6_dev $(M6_LIMIT_FLAGS)

m6-freeze:
	uv run python -m kodocs.eval.m6_config

M6_RERUN_FLAGS = $(if $(RERUN_REASON),--rerun-reason "$(RERUN_REASON)")

m6-test:
	uv run --group embed python -m kodocs.eval.m6_test $(M6_RERUN_FLAGS)

m6-tokens:
	uv run python -m kodocs.mcp_server.tokens

m6-serve:
	uv run python -m kodocs.mcp_server

m6-report:
	uv run python -m kodocs.eval.m6_report

m7-probe:
	uv run python -m kodocs.llm.gemini_probe

m7-cards:
	uv run python -m kodocs.eval.m7_human cards

m7-human-register:
	uv run python -m kodocs.eval.m7_human register $(M7_HUMAN_REGISTER_FLAGS)

M7_CREDIT_FLAGS = $(if $(M7_CREDIT_USD),--credit-usd $(M7_CREDIT_USD),)

m7-pilot:
	uv run python -m kodocs.eval.m7_grade pilot $(M7_CREDIT_FLAGS)

m7-freeze:
	uv run python -m kodocs.eval.m7_config

m7-grade-dry:
	uv run python -m kodocs.eval.m7_grade grade --dry-run $(M7_CREDIT_FLAGS)

m7-grade:
	uv run python -m kodocs.eval.m7_grade grade $(M7_CREDIT_FLAGS)

m7-analyze:
	uv run python -m kodocs.eval.m7_analyze

m7-evidence-check:
	uv run python -m kodocs.eval.m7_grading --check-evidence

m7-security-set:
	uv run --group embed python -m kodocs.eval.m7_security_set

m7-security-dry:
	uv run python -m kodocs.eval.m7_security --dry-run

m7-security:
	uv run --group embed python -m kodocs.eval.m7_security $(M6_RERUN_FLAGS) $(if $(MCP_URL),--mcp-url "$(MCP_URL)")

m7-leak-review-register:
	uv run python -m kodocs.eval.m7_security --register-review $(if $(CSV_PATH),--csv-path "$(CSV_PATH)") $(if $(FORCE_REASON),--force-reason "$(FORCE_REASON)")

m7-security-rejudge:
	uv run python -m kodocs.eval.m7_security --rejudge

m7-latency-dry:
	uv run python -m kodocs.eval.m7_latency --dry-run

m7-latency:
	uv run --group embed python -m kodocs.eval.m7_latency $(M6_RERUN_FLAGS)

m7-failures:
	uv run python -m kodocs.eval.m7_failures

m7-report:
	uv run python -m kodocs.eval.m7_report

m8-audit:
	uv run python -m kodocs.eval.m8_audit

m8-audit-gh:
	uv run python -m kodocs.eval.m8_audit_gh

adr-index:
	uv run python -m kodocs.eval.m8_adr_index

m8-wrap-report:
	uv run python -m kodocs.eval.m8_wrap_report

m8-parse-error:
	uv run python -m kodocs.eval.m8_parse_error

readme:
	uv run python -m kodocs.eval.m8_readme

M8_RERUN_FLAGS = $(if $(RERUN_REASON),--rerun-reason "$(RERUN_REASON)")

m8-stream-repro:
	uv run --group embed python -m kodocs.eval.m8_stream_repro $(M8_RERUN_FLAGS) $(if $(STAGE),--stage $(STAGE))

m8-stream-repro-dry:
	uv run --group embed python -m kodocs.eval.m8_stream_repro --dry-run $(if $(STAGE),--stage $(STAGE))
