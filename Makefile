.PHONY: verify up down logs test-integration collect-law collect-alio match-versions convert-hwp parse-targets parse-pypdf docling-models parse-docling

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
