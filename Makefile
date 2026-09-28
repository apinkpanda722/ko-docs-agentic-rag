.PHONY: verify up down logs test-integration collect-law collect-alio match-versions convert-hwp parse-targets parse-pypdf docling-models parse-docling parse-upstage-dry parse-upstage parse-upstage-direct-dry parse-upstage-direct parse-score table-drafts table-golden table-score parse-report reg-index seed-names build-db plan-docs generate-dry generate-pilot generate-pilot-rerun generate synth-validate synth-metrics synth-report review-sample review-score query-specs queries-dry queries

# 하네스의 단일 검증 진입점. Stop hook, scripts/execute.py, step AC가 모두 이 타깃을 호출한다.
verify:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest -q

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
