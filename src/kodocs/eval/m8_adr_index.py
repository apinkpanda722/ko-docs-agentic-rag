"""ADR status and cross-reference index, without rewriting decision history."""

import re
from pathlib import Path

STATUS_VALUES = frozenset(
    {"유효", "폐기", "유효(일부 변경: ADR-NNN)", "대체됨(→ ADR-NNN)"}
)
STATUS_FORMAT = re.compile(
    r"유효|폐기|유효\(일부 변경: ADR-\d{3}\)|대체됨\(→ ADR-\d{3}\)"
)
HEADING = re.compile(r"^### (ADR-\d{3})(?: \(폐기\))?: (.+)$", re.MULTILINE)
REFERENCE = re.compile(r"ADR-\d{3}(?!\d)")


def parse(document):
    headings = list(HEADING.finditer(document))
    entries = []
    for i, heading in enumerate(headings):
        body = document[
            heading.end() : headings[i + 1].start()
            if i + 1 < len(headings)
            else len(document)
        ]
        lines = body.splitlines()
        if len(lines) < 2 or not lines[1].startswith("**상태**: "):
            raise ValueError("ADR 제목 바로 아래 상태 필요")
        status = lines[1].removeprefix("**상태**: ").split(" — ", 1)[0]
        if not STATUS_FORMAT.fullmatch(status):
            raise ValueError("ADR 상태 형식 오류")
        title = heading.group(2)
        heading_text = heading.group().removeprefix("### ")
        anchor = re.sub(r"[^\w\- ]", "", heading_text.lower()).replace(" ", "-")
        entries.append(
            {
                "id": heading.group(1),
                "title": title,
                "status": status,
                "anchor": anchor,
                "outgoing": sorted(set(REFERENCE.findall(body))),
            }
        )
    ids = {e["id"] for e in entries}
    if not entries or len(ids) != len(entries):
        raise ValueError("ADR 제목 없음 또는 중복")
    for entry in entries:
        if not set(entry["outgoing"]) <= ids:
            raise ValueError("존재하지 않는 ADR 참조")
        entry["incoming"] = [e["id"] for e in entries if entry["id"] in e["outgoing"]]
    return entries


def render(entries):
    lines = [
        "이 파일은 `make adr-index`가 생성한다. 손으로 고치지 않는다.",
        "",
        "# ADR 색인",
        "",
        "| 번호 | 제목 | 상태 | 나가는 참조 | 들어오는 참조 |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| [{e['id']}](ADR.md#{e['anchor']}) | {e['title']} | {e['status']} | {', '.join(e['outgoing']) or '—'} | {', '.join(e['incoming']) or '—'} |"
        )
    return "\n".join(lines) + "\n"


def main():
    root = Path(".")
    (root / "docs/ADR-INDEX.md").write_text(
        render(parse((root / "docs/ADR.md").read_text())), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
