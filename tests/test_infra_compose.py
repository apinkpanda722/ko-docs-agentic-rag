"""Validate the local OpenSearch infrastructure configuration without Docker."""

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = PROJECT_ROOT / "infra" / "Dockerfile"
COMPOSE_FILE = PROJECT_ROOT / "infra" / "docker-compose.yml"


def _compose_config() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _opensearch_service() -> dict:
    return _compose_config()["services"]["opensearch"]


def test_dockerfile_pins_opensearch_3_8_0() -> None:
    """Keep the search engine version fixed so retrieval metrics are reproducible."""
    from_lines = [
        line.strip()
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip().upper().startswith("FROM ")
    ]

    assert from_lines == ["FROM opensearchproject/opensearch:3.8.0"], (
        "infra/Dockerfile must pin FROM to opensearchproject/opensearch:3.8.0"
    )


def test_dockerfile_installs_analysis_nori() -> None:
    """Ensure the non-bundled Korean analyzer is installed at image build time."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "opensearch-plugin install --batch analysis-nori" in dockerfile, (
        "infra/Dockerfile must install analysis-nori with opensearch-plugin --batch"
    )


def test_compose_disables_security_and_demo_config() -> None:
    """Keep both security and its unused demo credentials disabled locally."""
    environment = _opensearch_service()["environment"]

    assert environment.get("DISABLE_SECURITY_PLUGIN") == "true", (
        "opensearch must set DISABLE_SECURITY_PLUGIN=true"
    )
    assert environment.get("DISABLE_INSTALL_DEMO_CONFIG") == "true", (
        "opensearch must set DISABLE_INSTALL_DEMO_CONFIG=true"
    )


def test_compose_binds_opensearch_only_to_loopback() -> None:
    """Prevent the unauthenticated development endpoint from reaching the LAN."""
    ports = _opensearch_service()["ports"]

    assert ports == ["127.0.0.1:9200:9200"], (
        "opensearch port must be bound exactly as 127.0.0.1:9200:9200; "
        "0.0.0.0 and host-IP-less mappings are unsafe"
    )


def test_compose_uses_single_node_discovery() -> None:
    """Keep the local cluster in explicit single-node mode."""
    environment = _opensearch_service()["environment"]

    assert environment.get("discovery.type") == "single-node", (
        "opensearch must set discovery.type=single-node"
    )


def test_compose_uses_a_declared_named_data_volume() -> None:
    """Store reproducible index data in a Docker-managed volume, not a host path."""
    config = _compose_config()
    mounts = _opensearch_service()["volumes"]
    sources = [mount.split(":", maxsplit=1)[0] for mount in mounts]
    declared_volumes = config.get("volumes", {})

    assert sources, "opensearch must mount a named data volume"
    assert all(not source.startswith(("./", "/")) for source in sources), (
        "opensearch data mounts must not use ./ or absolute host bind paths"
    )
    assert all(source in declared_volumes for source in sources), (
        "every opensearch data volume must be declared in top-level volumes"
    )
