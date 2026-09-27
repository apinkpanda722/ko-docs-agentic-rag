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

## 인물 명단 검토

인물 이름은 성과 요즘식·순우리말 이름 풀을 조합해 만든 가상 이름이지만, 조합이
널리 알려진 실존 인물의 이름과 우연히 겹칠 수 있다. 시드를 바꾸거나 인물을 다시
만든 뒤에는 다음 절차로 명단을 확인한다.

1. `make seed-names`로 팀별 명단을 출력해 사람이 훑어본다.
2. 널리 알려진 인물(정치인·기업인·연예인·운동선수 등)과 겹치는 이름이 있으면
   `uv run python -m kodocs.synth.bootstrap_people --rename p-007 p-123`처럼
   해당 `person_id`만 지정해 이름을 다시 뽑는다. 결과는 결정론적이고 지정한 인물 외에는
   바뀌지 않는다.
3. 다른 시드 파일은 인물을 이름이 아니라 `person_id`로 참조하므로 따로 고칠 것이 없다.
   `make verify`로 확인한 뒤 커밋한다.

`banned_names.yaml`에는 합성 기록에 쓰면 안 되는 원천 기관명(닫힌 목록)만 둔다.
실존 인물 명단은 두지 않는다.
