# 로컬 OpenSearch

이 구성은 로컬 개발 전용 OpenSearch 단일 노드다. 베이스 이미지는
`opensearchproject/opensearch:3.8.0`으로 고정한다. Docker Hub의 OpenSearch 3.x
최신 안정 버전이며 같은 버전의 `analysis-nori` 아티팩트가 존재하기 때문이다.
버전을 고정하면 엔진 변경으로 BM25·hybrid 검색 수치가 조용히 달라지는 일을
막을 수 있다.

OpenSearch 3.0부터 hybrid 쿼리의 공통 filter가 k-NN 하위 쿼리에 효율적인 사전
필터로 적용된다(ADR-005). k-NN과 neural-search의 hybrid 및 normalization·
score-ranker processor는 기본 배포판에 번들되어 있다. 한국어 형태소 분석기인
`analysis-nori`만 Docker 이미지 빌드 시 추가로 설치한다.

## 요구 사항

- Docker Desktop에 메모리를 4GB 이상 할당한다. OpenSearch 힙은 2GB
  (`-Xms2g -Xmx2g`)로 고정되어 있다.
- Linux 호스트에서는 `vm.max_map_count`가 최소 `262144`인지 확인한다. 값이
  작으면 OpenSearch가 시작되지 않을 수 있으며, 필요하면 호스트에서
  `sudo sysctl -w vm.max_map_count=262144`로 조정한다.

## 실행

```bash
make up
# OpenSearch 기동이 끝날 때까지 기다린다.
make test-integration
make down
```

보안 플러그인과 데모 인증 설정을 모두 끈 무인증 구성이다. 포트는
`127.0.0.1:9200`에만 바인딩해 같은 네트워크의 다른 기기에 노출되지 않게 했다.
운영 또는 공유 환경에서는 이 구성을 사용하지 않는다.
