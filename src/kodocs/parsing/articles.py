"""Deterministically split parser-independent text into Korean rule articles.

FP-043 counts ``len(SplitResult.articles)``: every main ``제N조`` is one
article, including a separate ``제N조의M`` and a deleted article (삭제 조). Chapter and
section headings, articles inside 부칙, and text after 별표 markers are not main
articles. Duplicate or decreasing article numbers remain in input order so
structural errors stay visible to evaluation.
"""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from kodocs.parsing.schema import BlockKind, ParsedDocument

ARTICLE_RE = re.compile(
    r"^\s*제\s*(?P<number>\d+)\s*조"
    r"(?:\s*의\s*(?P<branch>\d+))?"
    r"(?:"
    r"\s*[(（]\s*(?P<paren_title>[^)）]{1,40}?)\s*[)）]"
    r"|\s*【\s*(?P<bracket_title>[^】]{1,40}?)\s*】"
    r"|\s*(?P<deleted>삭\s*제)"
    r")"
)
HEADING_RE = re.compile(r"^(?=.{1,40}$)\s*제\s*\d+\s*(?:장|절|관)(?!\s*의\s*\d)")
ADDENDUM_RE = re.compile(r"^\s*부\s*칙\b")
ANNEX_RE = re.compile(
    r"^\s*(?P<label>"
    r"(?P<open>[\[【<〔])\s*"
    r"(?P<kind>별\s*표|별\s*지|서\s*식|부\s*록)\s*"
    r"(?:제?\s*(?P<number>\d+)"
    r"(?:\s*의\s*(?P<branch>\d+))?\s*호?)?"
    r"[^\]】>〕]*"
    r"(?P<close>[\]】>〕])"
    r")"
)
PAGE_NUMBER_RES = (
    re.compile(r"^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$"),
    re.compile(r"^\s*\d{1,4}\s*/\s*\d{1,4}\s*$"),
    re.compile(r"^\s*-\s*\d+\s*-\s*$"),
)

_ANNEX_CLOSERS = {"[": "]", "【": "】", "<": ">", "〔": "〕"}
_NOISE_KINDS: frozenset[BlockKind] = frozenset({"header", "footer", "page_number"})


@dataclass(frozen=True)
class Article:
    """One numbered article, preserving its source text."""

    number: int
    branch: int
    title: str | None
    deleted: bool
    text: str
    first_page: int | None


@dataclass(frozen=True)
class AddendumBlock:
    """One 부칙 block and the articles numbered within it."""

    heading: str
    articles: tuple[Article, ...]
    text: str


@dataclass(frozen=True)
class AnnexMarker:
    """A bracketed 별표, 별지, 서식, or 부록 boundary."""

    kind: str
    label: str
    number: str | None
    page: int | None
    block_index: int


@dataclass(frozen=True)
class SplitResult:
    """All deterministic structural regions found in one document."""

    articles: tuple[Article, ...]
    headings: tuple[str, ...]
    addenda: tuple[AddendumBlock, ...]
    annexes: tuple[AnnexMarker, ...]
    preamble: str
    dropped_noise: int


@dataclass
class _ArticleBuilder:
    number: int
    branch: int
    title: str | None
    deleted: bool
    first_page: int | None
    lines: list[str]

    def freeze(self) -> Article:
        return Article(
            number=self.number,
            branch=self.branch,
            title=self.title,
            deleted=self.deleted,
            text="\n".join(self.lines),
            first_page=self.first_page,
        )


@dataclass
class _AddendumBuilder:
    heading: str
    articles: list[Article]
    lines: list[str]

    def freeze(self) -> AddendumBlock:
        return AddendumBlock(
            heading=self.heading,
            articles=tuple(self.articles),
            text="\n".join(self.lines),
        )


@dataclass(frozen=True)
class _DocumentLine:
    block_index: int
    page: int | None
    kind: BlockKind
    text: str


def split_document(doc: ParsedDocument) -> SplitResult:
    """Remove line-level parser noise, then split a common parsed document."""
    source_lines = [
        _DocumentLine(
            block_index=block_index,
            page=block.page,
            kind=block.kind,
            text=line,
        )
        for block_index, block in enumerate(doc.blocks)
        for line in _block_lines(block.text)
    ]
    kept_lines, dropped_noise = _drop_noise(source_lines, page_count=doc.page_count)
    result = split_text([(line.page, line.text) for line in kept_lines])
    annexes = tuple(
        replace(marker, block_index=kept_lines[marker.block_index].block_index)
        for marker in result.annexes
    )
    return replace(result, annexes=annexes, dropped_noise=dropped_noise)


def split_text(lines: Sequence[tuple[int | None, str]]) -> SplitResult:
    """Split ordered ``(page, line)`` pairs without changing their text."""
    main_articles: list[Article] = []
    headings: list[str] = []
    addenda: list[AddendumBlock] = []
    annexes: list[AnnexMarker] = []
    preamble_lines: list[str] = []

    current_article: _ArticleBuilder | None = None
    current_target: str | None = None
    active_addendum: _AddendumBuilder | None = None
    in_annex = False
    main_article_seen = False

    def finish_article() -> None:
        nonlocal current_article, current_target
        if current_article is None:
            return
        article = current_article.freeze()
        if current_target == "addendum":
            if active_addendum is None:
                raise RuntimeError("addendum article has no active block")
            active_addendum.articles.append(article)
        else:
            main_articles.append(article)
        current_article = None
        current_target = None

    def finish_addendum() -> None:
        nonlocal active_addendum
        if active_addendum is not None:
            addenda.append(active_addendum.freeze())
            active_addendum = None

    for block_index, (page, line) in enumerate(lines):
        annex_match = _annex_match(line)
        if annex_match is not None:
            finish_article()
            kind = re.sub(r"\s+", "", annex_match.group("kind"))
            number = _annex_number(annex_match)
            annexes.append(
                AnnexMarker(
                    kind=kind,
                    label=annex_match.group("label"),
                    number=number,
                    page=page,
                    block_index=block_index,
                )
            )
            in_annex = True
            continue

        if ADDENDUM_RE.match(line):
            finish_article()
            finish_addendum()
            active_addendum = _AddendumBuilder(
                heading=line,
                articles=[],
                lines=[line],
            )
            in_annex = False
            continue

        article_match = ARTICLE_RE.match(line)
        if article_match is not None:
            finish_article()
            in_annex = False
            title = article_match.group("paren_title") or article_match.group(
                "bracket_title"
            )
            deleted = article_match.group("deleted") is not None
            current_target = "addendum" if active_addendum is not None else "main"
            current_article = _ArticleBuilder(
                number=int(article_match.group("number")),
                branch=int(article_match.group("branch") or 0),
                title=title.strip() if title is not None else None,
                deleted=deleted,
                first_page=page,
                lines=[line],
            )
            if active_addendum is not None:
                active_addendum.lines.append(line)
            else:
                main_article_seen = True
            continue

        if in_annex:
            continue
        if HEADING_RE.match(line):
            finish_article()
            headings.append(line)
            continue
        if current_article is not None:
            current_article.lines.append(line)
            if active_addendum is not None:
                active_addendum.lines.append(line)
        elif active_addendum is not None:
            active_addendum.lines.append(line)
        elif not main_article_seen:
            preamble_lines.append(line)

    finish_article()
    finish_addendum()
    return SplitResult(
        articles=tuple(main_articles),
        headings=tuple(headings),
        addenda=tuple(addenda),
        annexes=tuple(annexes),
        preamble="\n".join(preamble_lines),
        dropped_noise=0,
    )


def law_to_lines(law: Mapping[object, object]) -> list[tuple[None, str]]:
    """Convert one cleaned Law Open API object into splitter input lines."""
    lines: list[tuple[None, str]] = []
    for article in _mapping_sequence(law, "articles"):
        lines.append((None, str(article)))

    for addendum_value in _mapping_sequence(law, "addenda"):
        addendum = _as_mapping(addendum_value, "addenda")
        text = str(addendum.get("text", ""))
        for line in text.splitlines() or [text]:
            lines.append((None, line))

    for annex_value in _mapping_sequence(law, "annexes"):
        annex = _as_mapping(annex_value, "annexes")
        number = int(str(annex.get("number", "")))
        branch_text = str(annex.get("branch", ""))
        rendered_number = str(number)
        if branch_text and int(branch_text) != 0:
            rendered_number += f"의{int(branch_text)}"
        kind = str(annex.get("kind", ""))
        title = str(annex.get("title", ""))
        lines.append((None, f"[{kind} {rendered_number}] {title}"))
    return lines


def _block_lines(text: str) -> tuple[str, ...]:
    lines = tuple(text.splitlines())
    return lines or (text,)


def _drop_noise(
    lines: Sequence[_DocumentLine],
    *,
    page_count: int | None,
) -> tuple[list[_DocumentLine], int]:
    dropped_indexes: set[int] = set()
    for index, line in enumerate(lines):
        if _is_boundary(line.text):
            continue
        if line.kind in _NOISE_KINDS or any(
            pattern.fullmatch(line.text) for pattern in PAGE_NUMBER_RES
        ):
            dropped_indexes.add(index)

    pages = {line.page for line in lines if line.page is not None}
    total_pages = page_count if page_count is not None else len(pages)
    if total_pages >= 3 and pages:
        candidates: dict[str, set[int]] = defaultdict(set)
        candidate_indexes: list[int] = []
        for page in pages:
            page_indexes = [
                index
                for index, line in enumerate(lines)
                if line.page == page and index not in dropped_indexes
            ]
            edge_indexes = page_indexes[:2] + page_indexes[-2:]
            for index in dict.fromkeys(edge_indexes):
                line = lines[index]
                if _is_boundary(line.text):
                    continue
                candidates[line.text].add(page)
                candidate_indexes.append(index)

        repeated = {
            text
            for text, seen_pages in candidates.items()
            if len(seen_pages) > total_pages / 2
        }
        dropped_indexes.update(
            index for index in candidate_indexes if lines[index].text in repeated
        )

    return (
        [line for index, line in enumerate(lines) if index not in dropped_indexes],
        len(dropped_indexes),
    )


def _is_boundary(text: str) -> bool:
    return bool(ARTICLE_RE.match(text) or ADDENDUM_RE.match(text) or _annex_match(text))


def _annex_match(text: str) -> re.Match[str] | None:
    match = ANNEX_RE.match(text)
    if match is None:
        return None
    if _ANNEX_CLOSERS[match.group("open")] != match.group("close"):
        return None
    return match


def _annex_number(match: re.Match[str]) -> str | None:
    number = match.group("number")
    if number is None:
        return None
    branch = match.group("branch")
    return number if branch is None else f"{number}의{branch}"


def _mapping_sequence(
    mapping: Mapping[object, object],
    field: str,
) -> Sequence[object]:
    value = mapping.get(field, ())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    raise TypeError(f"{field} must be a sequence")


def _as_mapping(value: object, field: str) -> Mapping[object, object]:
    if isinstance(value, Mapping):
        return value
    raise TypeError(f"{field} entries must be mappings")
