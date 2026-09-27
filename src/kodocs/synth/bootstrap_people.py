"""Create the deterministic, reviewable people seed files."""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Collection, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

import yaml

from kodocs.synth.seed import TEAMS, load_public_figures

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
GIVEN_FIRST_POOL = (
    "가",
    "건",
    "경",
    "나",
    "다",
    "도",
    "라",
    "민",
    "보",
    "서",
    "소",
    "수",
    "시",
    "아",
    "연",
    "예",
    "유",
    "은",
    "재",
    "지",
    "채",
    "태",
    "하",
    "해",
    "현",
)
GIVEN_SECOND_POOL = (
    "겸",
    "경",
    "나",
    "담",
    "람",
    "린",
    "민",
    "별",
    "빈",
    "서",
    "솔",
    "수",
    "아",
    "안",
    "연",
    "영",
    "온",
    "우",
    "원",
    "윤",
    "은",
    "인",
    "준",
    "진",
    "현",
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
    *,
    random_seed: int,
    team_sizes: Mapping[str, int],
    excluded_names: Collection[str] = (),
) -> dict[str, list[dict]]:
    """Return people in fixed team order and descending grade order.

    A drawn name in ``excluded_names`` is replaced from a separate generator
    keyed by the drawn name, so the main draw sequence (and every other person)
    stays the same as without the exclusion.
    """
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
    excluded = frozenset(excluded_names)
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
            name = _unique_name(generator, used_names, excluded, random_seed)
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


def main(argv: Sequence[str] | None = None) -> int:
    """Write one YAML file per team, refusing replacement without ``--force``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/seed/people"))
    parser.add_argument("--random-seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="public_figures를 읽을 저장소 루트(data/seed/banned_names.yaml)",
    )
    args = parser.parse_args(argv)

    targets = [args.out / f"{team}.yaml" for team in TEAMS]
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
        excluded_names=load_public_figures(args.root),
    )
    args.out.mkdir(parents=True, exist_ok=True)
    for team, target in zip(TEAMS, targets, strict=True):
        target.write_text(
            yaml.safe_dump(
                {"people": people[team]}, allow_unicode=True, sort_keys=False
            ),
            encoding="utf-8",
        )
    return 0


def _unique_name(
    generator: random.Random,
    used_names: set[str],
    excluded: frozenset[str],
    random_seed: int,
) -> str:
    capacity = len(SURNAME_POOL) * len(GIVEN_FIRST_POOL) * len(GIVEN_SECOND_POOL)
    if len(used_names) + len(excluded) >= capacity:
        raise ValueError("name pools do not contain enough unique combinations")
    while True:
        candidate = _draw_name(generator)
        if candidate in used_names:
            continue
        if candidate in excluded:
            candidate = _replacement_name(candidate, used_names, excluded, random_seed)
        used_names.add(candidate)
        return candidate


def _replacement_name(
    drawn: str, used_names: set[str], excluded: frozenset[str], random_seed: int
) -> str:
    # A separate generator keeps the main draw sequence unchanged.
    replacement_generator = random.Random(f"{random_seed}:replace:{drawn}")
    while True:
        candidate = _draw_name(replacement_generator)
        if candidate not in used_names and candidate not in excluded:
            return candidate


def _draw_name(generator: random.Random) -> str:
    return (
        generator.choice(SURNAME_POOL)
        + generator.choice(GIVEN_FIRST_POOL)
        + generator.choice(GIVEN_SECOND_POOL)
    )


if __name__ == "__main__":
    raise SystemExit(main())
