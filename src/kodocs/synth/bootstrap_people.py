"""Create the deterministic, reviewable people seed files."""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Collection, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

import yaml

from kodocs.synth.seed import GRADES, TEAMS

DEFAULT_RANDOM_SEED = 20250101
DEFAULT_TEAM_SIZES: Mapping[str, int] = {
    "platform": 42,
    "payments": 34,
    "data-ai": 30,
    "sre": 26,
    "security": 18,
    "hr": 18,
    "finance": 20,
    "legal": 12,
}

SURNAME_POOL = (
    "강",
    "고",
    "곽",
    "권",
    "김",
    "남",
    "노",
    "문",
    "박",
    "배",
    "서",
    "성",
    "송",
    "신",
    "안",
    "양",
    "오",
    "유",
    "윤",
    "이",
    "임",
    "장",
    "전",
    "정",
    "조",
    "차",
    "최",
    "한",
    "허",
    "홍",
)
# 요즘식·순우리말 두 글자 이름. 윗세대에 흔한 전통식 이름은 넣지 않는다
# (조합이 실존 공인 이름과 겹칠 가능성을 줄이기 위해서다. 최종 확인은 사람이 명단을 훑어본다).
GIVEN_NAME_POOL = (
    "하린",
    "서윤",
    "지우",
    "도윤",
    "시우",
    "하준",
    "예린",
    "다온",
    "라온",
    "하람",
    "나래",
    "가온",
    "새봄",
    "누리",
    "한별",
    "은솔",
    "다솜",
    "보람",
    "슬기",
    "이든",
    "로운",
    "윤슬",
    "해솔",
    "온유",
    "아린",
    "채원",
    "지안",
    "서아",
    "유나",
    "도하",
    "이안",
    "주원",
    "하율",
    "서진",
    "예준",
    "시아",
    "소율",
    "지호",
    "은우",
    "하온",
    "나은",
    "수아",
    "리안",
    "연우",
    "다인",
    "초아",
    "아윤",
    "로하",
    "윤하",
    "채아",
    "가람",
    "새론",
    "여름",
    "마루",
    "바다",
    "하늘",
    "별하",
    "단비",
    "이서",
    "민서",
    "규리",
    "세아",
    "도담",
    "라희",
    "소담",
    "해랑",
    "겨레",
    "한결",
    "다올",
    "온새",
    "해든",
    "새별",
    "봄솔",
    "다은",
    "서우",
    "유준",
    "루아",
    "이로",
    "이음",
    "하랑",
    "찬솔",
    "한솔",
    "푸름",
    "나린",
    "다해",
    "시온",
    "우주",
    "은하",
)

ROLE_POOLS: Mapping[str, tuple[str, ...]] = {
    "platform": ("백엔드 개발", "플랫폼 개발", "개발 생산성", "API 설계"),
    "payments": ("결제 개발", "정산 개발", "청구 운영", "결제 품질"),
    "data-ai": ("데이터 엔지니어링", "분석 모델링", "ML 플랫폼", "데이터 품질"),
    "sre": ("사이트 신뢰성", "클라우드 운영", "관측성", "배포 자동화"),
    "security": ("보안 관제", "제품 보안", "접근 통제", "보안 거버넌스"),
    "hr": ("인재 운영", "조직문화", "채용", "교육 기획"),
    "finance": ("재무 회계", "자금 운영", "경영 관리", "구매 정산"),
    "legal": ("계약 검토", "준법 지원", "분쟁 대응", "개인정보 지원"),
}

_JOIN_START = date(2012, 1, 1)
_JOIN_END = date(2026, 5, 31)


def generate_people(
    *, random_seed: int, team_sizes: Mapping[str, int]
) -> dict[str, list[dict]]:
    """Return people in fixed team order and descending grade order."""
    unknown = set(team_sizes).difference(TEAMS)
    missing = set(TEAMS).difference(team_sizes)
    if unknown or missing:
        raise ValueError(
            f"team_sizes must match fixed teams (unknown={sorted(unknown)}, "
            f"missing={sorted(missing)})"
        )
    if any(not isinstance(size, int) or size < 1 for size in team_sizes.values()):
        raise ValueError("team sizes must be positive integers")

    generator = random.Random(random_seed)
    used_names: set[str] = set()
    pending: dict[str, list[dict[str, object]]] = {}
    join_span = (_JOIN_END - _JOIN_START).days

    for team in TEAMS:
        grades = [5]
        if team in {"platform", "finance"}:
            grades.insert(0, 6)
        grades.extend(
            generator.choices(
                population=(1, 2, 3, 4),
                weights=(30, 30, 25, 15),
                k=team_sizes[team] - len(grades),
            )
        )
        records: list[dict[str, object]] = []
        for sequence, grade in enumerate(grades):
            name = _unique_name(generator, used_names)
            records.append(
                {
                    "name": name,
                    "team": team,
                    "grade": grade,
                    "role": generator.choice(ROLE_POOLS[team]),
                    "joined": (
                        _JOIN_START + timedelta(days=generator.randint(0, join_span))
                    ).isoformat(),
                    "_sequence": sequence,
                }
            )
        records.sort(
            key=lambda person: (-int(person["grade"]), int(person["_sequence"]))
        )
        pending[team] = records

    identifier = 1
    result: dict[str, list[dict]] = {}
    for team in TEAMS:
        result[team] = []
        for record in pending[team]:
            record.pop("_sequence")
            result[team].append({"person_id": f"p-{identifier:03d}", **record})
            identifier += 1
    return result


def rename_people(
    people: Mapping[str, Sequence[Mapping[str, object]]],
    person_ids: Collection[str],
    *,
    random_seed: int,
) -> tuple[dict[str, list[dict]], list[tuple[str, str, str]]]:
    """Redraw names for ``person_ids`` only; every other record stays unchanged.

    Each redraw uses its own generator keyed by the person ID, so the result
    does not depend on which other people are renamed in the same call.
    """
    known = {
        str(person["person_id"]) for records in people.values() for person in records
    }
    unknown = sorted(set(person_ids).difference(known))
    if unknown:
        raise ValueError(f"unknown person_id: {', '.join(unknown)}")

    used_names = {
        str(person["name"]) for records in people.values() for person in records
    }
    renamed: dict[str, list[dict]] = {
        team: [dict(person) for person in records] for team, records in people.items()
    }
    changes: list[tuple[str, str, str]] = []
    for person_id in sorted(set(person_ids)):
        generator = random.Random(f"{random_seed}:rename:{person_id}")
        record = next(
            person
            for records in renamed.values()
            for person in records
            if person["person_id"] == person_id
        )
        new_name = _unique_name(generator, used_names)
        changes.append((person_id, str(record["name"]), new_name))
        record["name"] = new_name
    return renamed, changes


def format_roster(people: Mapping[str, Sequence[Mapping[str, object]]]) -> str:
    """Render people grouped by team for a quick human scan of the names."""
    lines: list[str] = []
    total = 0
    for team in TEAMS:
        records = people.get(team, ())
        total += len(records)
        lines.append(f"[{team}] {len(records)}명")
        cells = [
            f"{person['person_id']} {person['name']}({GRADES[int(person['grade'])]})"
            for person in records
        ]
        for index in range(0, len(cells), 5):
            lines.append("  " + "  ".join(cells[index : index + 5]))
    lines.append(f"총 {total}명")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Write, rename, or list the per-team people seed files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/seed/people"))
    parser.add_argument("--random-seed", type=int, default=DEFAULT_RANDOM_SEED)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true")
    mode.add_argument(
        "--rename",
        nargs="+",
        metavar="PERSON_ID",
        help="기존 명단에서 지정한 인물의 이름만 결정론적으로 다시 뽑는다",
    )
    mode.add_argument("--list", action="store_true", help="팀별 명단을 출력한다")
    args = parser.parse_args(argv)

    targets = [args.out / f"{team}.yaml" for team in TEAMS]
    if args.list or args.rename:
        missing = [path for path in targets if not path.exists()]
        if missing:
            print(f"인물 시드가 없습니다: {missing[0]}", file=sys.stderr)
            return 1
        people = {
            team: _read_people(target)
            for team, target in zip(TEAMS, targets, strict=True)
        }
        if args.list:
            print(format_roster(people))
            return 0
        try:
            renamed, changes = rename_people(
                people, args.rename, random_seed=args.random_seed
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        _write_people(renamed, targets)
        for person_id, old, new in changes:
            print(f"{person_id}: {old} -> {new}")
        return 0

    existing = [path for path in targets if path.exists()]
    if existing and not args.force:
        print(
            f"기존 인물 시드를 덮어쓰지 않습니다: {existing[0]} (--force 필요)",
            file=sys.stderr,
        )
        return 1

    people = generate_people(
        random_seed=args.random_seed,
        team_sizes=DEFAULT_TEAM_SIZES,
    )
    _write_people(people, targets)
    return 0


def _read_people(path: Path) -> list[dict]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(payload["people"])


def _write_people(
    people: Mapping[str, Sequence[Mapping]], targets: Sequence[Path]
) -> None:
    targets[0].parent.mkdir(parents=True, exist_ok=True)
    for team, target in zip(TEAMS, targets, strict=True):
        target.write_text(
            yaml.safe_dump(
                {"people": list(people[team])}, allow_unicode=True, sort_keys=False
            ),
            encoding="utf-8",
        )


def _unique_name(generator: random.Random, used_names: set[str]) -> str:
    capacity = len(SURNAME_POOL) * len(GIVEN_NAME_POOL)
    if len(used_names) >= capacity:
        raise ValueError("name pools do not contain enough unique combinations")
    while True:
        candidate = _draw_name(generator)
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate


def _draw_name(generator: random.Random) -> str:
    return generator.choice(SURNAME_POOL) + generator.choice(GIVEN_NAME_POOL)


if __name__ == "__main__":
    raise SystemExit(main())
