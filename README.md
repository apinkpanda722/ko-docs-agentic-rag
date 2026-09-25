# ko-docs-agentic-rag

한국어 사내 문서를 대상으로 한 Agentic RAG와 평가 하네스.

> 프로젝트 내용(목표, 아키텍처, 평가 지표)은 `docs/`에 정리한다. 이 README는 구현이 진행되면서 결과 수치와 함께 갱신한다.

## 개발 방식

[harness_framework_for_test](https://github.com/apinkpanda722/harness_framework_for_test)의 파일을 복사해 시작했다. Claude Code에서 `/harness`로 phase와 step을 설계하고, `scripts/execute.py`가 step마다 Codex(`codex exec`)로 코드를 수정한 뒤 `make verify`로 검증하고 커밋한다. 하네스 사용법은 [`scripts/README.md`](scripts/README.md)를 참고한다.
