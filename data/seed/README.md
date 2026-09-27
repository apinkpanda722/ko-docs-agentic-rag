# 합성 코퍼스 시드

이 디렉터리는 해온클라우드 합성 문서와 정형 원장의 공통 사실 원본이다.

- `company.yaml`: 회사, 생성 시드, 대상 기간
- `teams.yaml`: 고정 팀 코드와 목표 인원
- `people/`: 팀별 인물 레코드
- `services.yaml`: 서비스 소유권과 운영 사실
- `incidents/`: 파일당 장애 한 건
- `events/`: 프로젝트, 결정, 배포, 정책, 조직, 제한 이벤트
- `ledger/`: 티켓·결재 앵커와 배경 행 생성 분포
- `regulation_refs.yaml`: 색인으로 확인한 규정명·조 키와 참조 맥락
- `banned_names.yaml`: 합성 기록에서 거부할 실존 기관명

수정 뒤에는 `make verify`로 수량과 참조 무결성을 검증한다. 인물 시드를 다시
만들 때는 `python -m kodocs.synth.bootstrap_people --out data/seed/people --force`처럼
명시적으로 `--force`를 지정해야 한다.
