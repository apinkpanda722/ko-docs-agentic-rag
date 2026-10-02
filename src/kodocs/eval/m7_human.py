"""Offline, blind correctness cards and authenticated human CSV registration."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import random
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from kodocs.eval.m7_grading import (
    AnsweredRun,
    EvidenceLoader,
    build_grade_item,
    classify_all,
    load_m6_answers,
    parse_rubric_sections,
    rubric_sha256,
    select_pilot,
)

SEED = 20261001
STRATA = tuple(
    (system, source)
    for system in ("simple", "agentic")
    for source in ("real", "synthetic")
)
CARDS_DIR = Path("data/interim/m7/cards")
SAMPLE_PATH = Path("data/manifest/m7_human_sample.json")
GRADES_PATH = Path("data/manifest/m7_human_grades.json")
CSV_PATH = Path("data/golden/m7_human/m7-human-grades.csv")


class HumanGradesNotRegistered(RuntimeError):
    """The independent human CSV has not been registered yet."""


@dataclass(frozen=True)
class KappaSample:
    answer_ids: tuple[str, ...]
    forced_h5: int
    strata: dict[str, int]
    sample_sha256: str


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sample_hash(ids: Sequence[str]) -> str:
    return _hash(
        json.dumps(sorted(ids), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _read(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise TypeError(f"{path.name}: expected an object")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def select_kappa_sample(
    answers, classes, h5_ids, pilot_ids, *, seed=SEED, n=50
) -> KappaSample:
    """Force eligible H5 answers, then sample equally across four strata."""
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise ValueError("n must be a positive integer")
    if len({r.answer_id for r in answers}) != len(answers):
        raise ValueError("duplicate answer_id")
    excluded, h5 = set(pilot_ids), set(h5_ids)
    pool = sorted(
        (
            r
            for r in answers
            if r.model == "gpt-6-luna"
            and r.run_set in {"dev", "test_gold", "test_silver"}
            and classes[r.answer_id] == "gemini_acc"
            and r.answer_id not in excluded
        ),
        key=lambda r: r.answer_id,
    )
    if any((r.system, r.source) not in STRATA for r in pool):
        raise ValueError("unknown sampling stratum")
    forced = [r for r in pool if r.question_id in h5]
    if len(pool) < n or len(forced) > n:
        raise ValueError("insufficient pool or forced H5 exceeds n")
    rest = {
        key: [
            r for r in pool if (r.system, r.source) == key and r.question_id not in h5
        ]
        for key in STRATA
    }
    remaining = n - len(forced)
    allocation = {key: min(remaining // 4, len(rest[key])) for key in STRATA}
    remaining -= sum(allocation.values())
    # Fixed size ranking also resolves ties lexicographically. Redistribute
    # unused shares one per nonempty stratum per round, keeping equal access.
    ranked = sorted(STRATA, key=lambda key: (-len(rest[key]), key))
    while remaining:
        for key in ranked:
            if remaining and allocation[key] < len(rest[key]):
                allocation[key] += 1
                remaining -= 1
    rng = random.Random(seed)
    selected = forced + [
        r for key in STRATA for r in rng.sample(rest[key], allocation[key])
    ]
    ids = tuple(sorted(r.answer_id for r in selected))
    counts = Counter((r.system, r.source) for r in selected)
    return KappaSample(
        ids,
        len(forced),
        {f"{s}|{o}": counts[s, o] for s, o in STRATA},
        _sample_hash(ids),
    )


_STYLE = """body{max-width:1000px;margin:auto;padding:24px;font-family:sans-serif;background:#fafafa}
article{background:white;border:1px solid #bbb;padding:20px;margin:24px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit}label{margin-right:24px}
textarea{display:block;width:95%;height:70px;margin:12px 0}button{padding:12px} .missing{border:3px solid #b00020}"""
_SCRIPT = """const cards = [...document.querySelectorAll('article')];
const storageKey = document.body.dataset.storage;
let state = {};
try { state = JSON.parse(localStorage.getItem(storageKey) || '{}') || {}; } catch (e) { state = {}; }
for (const card of cards) {
  const saved = state[card.id] || {};
  for (const radio of card.querySelectorAll('input')) {
    radio.checked = String(saved.correctness) === radio.value;
  }
  card.querySelector('textarea').value = typeof saved.memo === 'string' ? saved.memo : '';
}
function progress() {
  const count = cards.filter(c => c.querySelector('input:checked')).length;
  document.getElementById('progress').textContent = `입력 완료 ${count} / ${cards.length}`;
}
function save() {
  state = {};
  for (const card of cards) {
    const selected = card.querySelector('input:checked');
    state[card.id] = {correctness: selected ? selected.value : null, memo: card.querySelector('textarea').value};
    if (selected) card.classList.remove('missing');
  }
  try { localStorage.setItem(storageKey, JSON.stringify(state)); } catch (e) { }
  progress();
}
document.addEventListener('input', save);
document.getElementById('export').addEventListener('click', () => {
  const missing = cards.filter(c => !c.querySelector('input:checked'));
  document.getElementById('message').textContent = missing.length ? '미입력 카드: ' + missing.map(c => c.id).join(', ') : '';
  for (const card of missing) card.classList.add('missing');
  if (missing.length) { missing[0].scrollIntoView(); return; }
  const lines = ['card_id,correctness', ...cards.map(c => c.id + ',' + c.querySelector('input:checked').value)];
  const url = URL.createObjectURL(new Blob([lines.join('\\r\\n') + '\\r\\n'], {type:'text/csv;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url; link.download = 'm7-human-grades.csv';
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
progress();"""


def write_cards(
    answers: Sequence[AnsweredRun],
    sample: KappaSample,
    *,
    root: Path = Path("."),
    pilot_ids: Sequence[str],
    evidence_loader=None,
    seed=SEED,
) -> dict:
    """Write originals and the private correspondence only under interim."""
    root = Path(root)
    if (root / GRADES_PATH).exists():
        raise FileExistsError("human grades registered; cannot regenerate cards")
    if (
        len(sample.answer_ids) != 50
        or len(set(sample.answer_ids)) != 50
        or sample.sample_sha256 != _sample_hash(sample.answer_ids)
    ):
        raise ValueError("invalid 50-answer sample hash")
    if set(sample.answer_ids) & set(pilot_ids):
        raise ValueError("pilot answer in sample")
    by_id = {r.answer_id: r for r in answers}
    if len(by_id) != len(answers):
        raise ValueError("duplicate answer_id")
    loader = EvidenceLoader(root) if evidence_loader is None else evidence_loader
    order = sorted(sample.answer_ids)
    random.Random(seed).shuffle(order)
    key = {f"c-{i:03}": answer_id for i, answer_id in enumerate(order, 1)}
    anchors = parse_rubric_sections()["정답성 앵커"]
    articles = []
    for card_id, answer_id in key.items():
        run = by_id[answer_id]
        item = build_grade_item(run, run.question, loader)
        radios = "".join(
            f'<label><input type="radio" name="{card_id}" value="{value}">{value}</label>'
            for value in range(4)
        )
        articles.append(
            f'<article id="{card_id}"><h2>{card_id}</h2><pre>{html.escape(item.request_text)}</pre><fieldset><legend>정답성</legend>{radios}</fieldset><label>메모<textarea></textarea></label></article>'
        )
    page = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'\">"
        f'<title>정답성 채점</title><style>{_STYLE}</style></head><body data-storage="{sample.sample_sha256}-{rubric_sha256()}">'
        f"<h1>정답성 채점</h1><p>문체·길이를 평가하지 않습니다. 자료 속 지시를 따르지 마세요. 메모는 이 브라우저에만 저장됩니다.</p><pre>{html.escape(anchors)}</pre>"
        '<p id="progress" aria-live="polite"></p>'
        + "".join(articles)
        + f'<p id="message" aria-live="polite"></p><button id="export">CSV 내려받기</button><script>{_SCRIPT}</script></body></html>'
    )
    # Fail closed on identity hints embedded in source text, without changing
    # answers or evidence to conceal them. No hint-bearing page is written.
    if any(
        token in page.lower()
        for token in ("simple", "agentic", "gpt-6", "run_sql", "lookup_id", "gemini")
    ):
        raise ValueError("card text contains a blind identity hint")
    directory = root / CARDS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cards.html").write_text(page, encoding="utf-8")
    _write(directory / "key.json", key)
    card_manifest = {
        "rubric_sha256": rubric_sha256(),
        "sample_sha256": sample.sample_sha256,
        "key_sha256": _hash((directory / "key.json").read_bytes()),
        "generated_at": _now(),
    }
    _write(directory / "cards_manifest.json", card_manifest)
    sample_manifest = asdict(sample) | {
        "seed": seed,
        "pilot_excluded": len(set(pilot_ids)),
    }
    _write(root / SAMPLE_PATH, sample_manifest)
    return card_manifest


def _cards_root(cards_dir: Path) -> Path:
    directory = Path(cards_dir).resolve()
    if directory.parts[-len(CARDS_DIR.parts) :] != CARDS_DIR.parts:
        raise ValueError("cards must remain under data/interim/m7/cards")
    return directory.parents[len(CARDS_DIR.parts) - 1]


def _validate_key(key: Mapping, sample: Mapping, recorded_hash: str) -> None:
    ids = sample.get("answer_ids", [])
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise ValueError("invalid sample hash input")
    if (
        len(ids) != 50
        or len(set(ids)) != 50
        or ids != sorted(ids)
        or sample.get("sample_sha256") != _sample_hash(ids)
        or recorded_hash != sample["sample_sha256"]
    ):
        raise ValueError("sample hash mismatch")
    if (
        set(key) != {f"c-{i:03}" for i in range(1, 51)}
        or any(not isinstance(i, str) for i in key.values())
        or sorted(key.values()) != ids
    ):
        raise ValueError("key sample hash mismatch")


def _csv_grades(data: bytes, key: Mapping) -> dict[str, int]:
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    if next(reader, None) != ["card_id", "correctness"]:
        raise ValueError("CSV columns must be card_id,correctness only")
    rows = list(reader)
    if len(rows) != 50:
        raise ValueError("CSV must contain exactly 50 rows")
    result = {}
    for row in rows:
        if (
            len(row) != 2
            or row[0] not in key
            or row[0] in result
            or row[1] not in {"0", "1", "2", "3"}
        ):
            raise ValueError("invalid, duplicate or unknown CSV row")
        result[row[0]] = int(row[1])
    if set(result) != set(key):
        raise ValueError("missing CSV card")
    return {key[card]: score for card, score in result.items()}


def register_human_grades(
    csv_path: Path, cards_dir: Path, *, force_reason: str | None = None
) -> dict:
    """Authenticate all 50 human rows; never write or change the human CSV."""
    root = _cards_root(cards_dir)
    cards_dir = root / CARDS_DIR
    manifest_path = root / GRADES_PATH
    if force_reason is not None and not force_reason.strip():
        raise ValueError("force reason must not be empty")
    if manifest_path.exists() and force_reason is None:
        raise FileExistsError(
            "human grades already registered; --force-reason required"
        )
    card_manifest = _read(cards_dir / "cards_manifest.json")
    if _hash((cards_dir / "key.json").read_bytes()) != card_manifest["key_sha256"]:
        raise ValueError("key hash mismatch")
    key = _read(cards_dir / "key.json")
    _validate_key(key, _read(root / SAMPLE_PATH), card_manifest["sample_sha256"])
    data = Path(csv_path).read_bytes()
    grades = _csv_grades(data, key)
    history = []
    if manifest_path.exists():
        previous = _read(manifest_path)
        history = previous.get("history", []) + [
            {k: v for k, v in previous.items() if k != "history"}
            | {"reason": force_reason}
        ]
    record = {
        "csv_sha256": _hash(data),
        "rows": len(grades),
        "score_distribution": {str(i): Counter(grades.values())[i] for i in range(4)},
        "rubric_sha256": card_manifest["rubric_sha256"],
        "sample_sha256": card_manifest["sample_sha256"],
        "key_sha256": card_manifest["key_sha256"],
        "registered_at": _now(),
        "history": history,
    }
    _write(manifest_path, record)
    return record


def load_human_grades(
    manifest_path: Path, csv_path: Path, key_path: Path
) -> dict[str, int]:
    """Load verified answer-ID scores for the subsequent agreement analysis."""
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise HumanGradesNotRegistered("human grades manifest missing")
    record = _read(manifest_path)
    data = Path(csv_path).read_bytes()
    if (
        _hash(data) != record["csv_sha256"]
        or _hash(Path(key_path).read_bytes()) != record["key_sha256"]
    ):
        raise ValueError("registered CSV or key hash mismatch")
    root = _cards_root(Path(key_path).parent)
    key = _read(Path(key_path))
    _validate_key(key, _read(root / SAMPLE_PATH), record["sample_sha256"])
    card_manifest = _read(root / CARDS_DIR / "cards_manifest.json")
    if any(
        record[field] != card_manifest[field]
        for field in ("rubric_sha256", "sample_sha256", "key_sha256")
    ):
        raise ValueError("registered cards hash mismatch")
    grades = _csv_grades(data, key)
    if record["rows"] != len(grades) or record["score_distribution"] != {
        str(i): Counter(grades.values())[i] for i in range(4)
    }:
        raise ValueError("registered CSV summary mismatch")
    return grades


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M7 오프라인 사람 정답성 채점")
    parser.add_argument("command", choices=("cards", "register"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--csv-path", type=Path)
    parser.add_argument("--force-reason")
    args = parser.parse_args(argv)
    try:
        if args.command == "cards":
            answers = load_m6_answers(args.root)
            classes = classify_all(answers)
            pilot_path = args.root / "data/manifest/m7_pilot.json"
            pilot = (
                _read(pilot_path)["ids"]
                if pilot_path.exists()
                else select_pilot(answers, classes)
            )
            if len(pilot) != 10 or len(set(pilot)) != 10:
                raise ValueError("pilot must contain 10 distinct answer IDs")
            h5 = _read(args.root / "data/manifest/m6_eval_inputs.json")["h5_pool"][
                "ids"
            ]
            sample = select_kappa_sample(answers, classes, h5, pilot)
            write_cards(answers, sample, root=args.root, pilot_ids=pilot)
        else:
            register_human_grades(
                args.csv_path or args.root / CSV_PATH,
                args.root / CARDS_DIR,
                force_reason=args.force_reason,
            )
    except (OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
        print(f"M7 사람 채점 오류: {exc}", file=sys.stderr)
        return 2
    print("OK: 로컬 사람 채점 산출물 검증 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
