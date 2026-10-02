"""Read-only, value-free public readiness inventory; findings are not verdicts."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from kodocs.synth.seed import find_banned_names, load_banned_names

MANIFEST = Path("data/manifest/m8_audit.json")
REPORT = Path("docs/results/2026-10-03-m8-public-readiness.md")
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"
MAX_BLOB = 5 * 1024 * 1024
GIT_COMMANDS = frozenset(
    {"ls-files", "rev-list", "cat-file", "log", "for-each-ref", "remote"}
)
PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "naver.com",
        "daum.net",
        "hanmail.net",
        "outlook.com",
        "hotmail.com",
        "icloud.com",
        "yahoo.com",
        "proton.me",
        "protonmail.com",
        "me.com",
    }
)
# Single pattern table. Matches remain in memory; only counts leave scan_text.
PATTERNS = {
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "openai": re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    "google": re.compile(r"AIza[A-Za-z0-9_-]{30,}"),
    "upstage": re.compile(r"\bup_[A-Za-z0-9_-]{20,}"),
    "law_oc": re.compile(r'[?&]OC=[^\s&#"\'<>]+', re.IGNORECASE),
    "assignment": re.compile(
        r"""(?i)\b(?:[A-Z0-9_]*(?:api_key|apikey|secret|token))\b["']?\s*[:=]\s*["']?([A-Za-z0-9_./+-]{12,})"""
    ),
    "mcp_plaintext": re.compile(
        r"""(?i)\btoken["']?\s*[:=]\s*["']([A-Za-z0-9_-]{43})["']"""
    ),
    "phone": re.compile(r"(?<!\d)(?:0\d{1,2}[- .]\d{3,4}[- .]\d{4}|010\d{8})(?!\d)"),
    "personal_field": re.compile(
        r"담당자명|담당자전화|전화번호|담당자연락처|담당자성명|담당자전화번호"
    ),
    "absolute_path": re.compile(r'(?:/Users/|/home/|[A-Za-z]:\\Users\\)[^\s"\'<>]+'),
}
HASH_TOKEN = re.compile(r"(?<![a-z0-9])[0-9a-f]{7,40}(?![a-z0-9])")
COMMIT_KEY = re.compile(r"커밋|commit", re.IGNORECASE)
INVENTORY_DEFINITION = "서로 다른 (파일, 해시) 쌍: 커밋|commit JSON 키의 값 또는 커밋|commit|code_commit이 있는 줄의 7~40자 소문자 16진 토큰. SHA-256은 제외."


class AuditError(RuntimeError):
    """Deliberately contains no subprocess output, input value or exception detail."""


def git(
    root: Path, *args: str, input_data: bytes | None = None, missing_ok=False
) -> bytes | None:
    if (
        not args
        or args[0] not in GIT_COMMANDS
        or (args[0] == "remote" and args[1:] != ("get-url", "origin"))
    ):
        raise AuditError("git 읽기 명령 허용 목록 위반")
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, input=input_data, capture_output=True, check=False
        )
    except (OSError, ValueError):
        raise AuditError("git 실행 오류") from None
    if result.returncode:
        if missing_ok:
            return None
        raise AuditError("git 읽기 오류")
    return result.stdout


def read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise AuditError("입력 읽기 오류") from None


def decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8") if b"\0" not in data else None
    except UnicodeError:
        return None


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def shingle_hashes(text: str) -> set[bytes]:
    text = normalize(text)
    return {
        hashlib.sha256(text[i : i + 30].encode()).digest()
        for i in range(len(text) - 29)
    }


def scan_text(text: str, reference: set[bytes] | None = None) -> dict:
    counts = {}
    for name, pattern in PATTERNS.items():
        matches = list(pattern.finditer(text))
        if name == "assignment":
            matches = [
                m
                for m in matches
                if not re.fullmatch(r"[0-9a-fA-F]{12,}|[0-9a-fA-F-]{36}", m[1])
            ]
        if matches:
            counts[name] = len(matches)
    overlap = longest = run = 0
    if reference:
        text = normalize(text)
        for i in range(len(text) - 29):
            hit = hashlib.sha256(text[i : i + 30].encode()).digest() in reference
            run = run + 1 if hit else 0
            overlap += int(hit)
            longest = max(longest, run + 29 if run else 0)
    return {
        "patterns": counts,
        "overlap_shingles": overlap,
        "max_overlap_chars": longest,
    }


def safe_path(path: str) -> str:
    # Even a malicious filename must not re-disclose a credential or email local part.
    for pattern in PATTERNS.values():
        path = pattern.sub("[redacted]", path)
    return re.sub(r"[^/\s@]+@([A-Za-z0-9.-]+)", r"[redacted]@\1", path)


def load_sources(root: Path) -> tuple[set[bytes], dict]:
    reference: set[bytes] = set()
    sources = []
    for directory in ("data/interim/law", "data/interim/parsed"):
        base = root / directory
        if not base.is_dir():
            continue
        files = sorted(base.rglob("*.json"))
        used = 0
        for path in files:
            try:
                payload = json.loads(read(path))
                texts = (
                    payload.get("articles", [])
                    if directory.endswith("/law")
                    else [b["text"] for b in payload.get("blocks", [])]
                )
                if not isinstance(texts, list) or not all(
                    isinstance(t, str) for t in texts
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise AuditError("로컬 원문 형식 오류") from None
            for text in texts:
                reference.update(shingle_hashes(text))
            used += 1
        sources.append({"path": directory, "files": used})
    return reference, {
        "status": "available" if reference else "skipped: 로컬 원문 없음",
        "sources": sources,
    }


def path_kinds(path: str) -> list[str]:
    p = Path(path)
    kinds = []
    if p.name != ".env.example" and (p.name == ".env" or p.name.startswith(".env.")):
        kinds.append("env")
    for prefix, kind in [
        ("data/raw/", "raw"),
        ("data/interim/", "interim"),
        ("data/golden/tables/", "golden_tables"),
    ]:
        if path.startswith(prefix) and not (
            kind == "golden_tables" and p.name == "README.md"
        ):
            kinds.append(kind)
    if p.name == "tokens.json":
        kinds.append("tokens_json")
    if p.suffix.lower() in {".hwp", ".hwpx", ".pdf", ".sqlite", ".sqlite3", ".db"}:
        kinds.append("original_or_database")
    return kinds


def path_inventory(paths: set[str]) -> dict:
    result = {}
    for path in sorted(paths):
        for kind in path_kinds(path):
            item = result.setdefault(kind, {"count": 0, "paths": []})
            item["count"] += 1
            item["paths"].append(safe_path(path))
    return result


def email_inventory(root: Path) -> dict:
    result = {}
    for role, fmt in [("author", "%ae"), ("committer", "%ce")]:
        counts = Counter()
        for email in (
            git(root, "log", "--all", f"--format={fmt}")
            .decode("utf-8", errors="replace")
            .splitlines()
        ):
            domain = email.rsplit("@", 1)[-1].lower() if "@" in email else "invalid"
            if (
                not re.fullmatch(r"[a-z0-9.-]+", domain)
                or scan_text(domain)["patterns"]
            ):
                domain = "invalid"
            counts[domain] += 1
        result[role] = {
            d: {
                "count": n,
                "category": "personal_service" if d in PERSONAL_DOMAINS else "other",
            }
            for d, n in sorted(counts.items())
        }
    return result


def commit_inventory(root: Path, tracked: list[str]) -> dict:
    pairs: set[tuple[str, str]] = set()
    calls = []
    candidates = set(tracked)
    for directory, pattern in (
        ("src", "**/*"),
        ("data/manifest", "*.json"),
        ("docs/results", "*.md"),
    ):
        candidates.update(
            p.relative_to(root).as_posix()
            for p in (root / directory).glob(pattern)
            if p.is_file() and not p.is_symlink()
        )
    for path in sorted(candidates):
        if not (
            path.startswith("src/")
            or (path.startswith("data/manifest/") and path.endswith(".json"))
            or (path.startswith("docs/results/") and path.endswith(".md"))
        ):
            continue
        text = decode(read(root / path))
        if text is None:
            continue
        if path.startswith("src/"):
            calls.extend(
                {"path": safe_path(path), "line": i}
                for i, line in enumerate(text.splitlines(), 1)
                if "rev-parse" in line
            )
            continue
        if path.endswith(".json"):
            try:
                payload = json.loads(text)
            except ValueError:
                raise AuditError("manifest JSON 형식 오류") from None

            def visit(value, path=path):
                if isinstance(value, dict):
                    for key, nested in value.items():
                        if (
                            COMMIT_KEY.search(key)
                            and isinstance(nested, str)
                            and HASH_TOKEN.fullmatch(nested)
                        ):
                            pairs.add((path, nested))
                        visit(nested)
                elif isinstance(value, list):
                    for nested in value:
                        visit(nested)

            visit(payload)
        else:
            for line in text.splitlines():
                if COMMIT_KEY.search(line):
                    pairs.update((path, m.group()) for m in HASH_TOKEN.finditer(line))
    existing = sum(
        git(root, "cat-file", "-e", f"{sha}^{{commit}}", missing_ok=True) is not None
        for sha in sorted({h for _, h in pairs})
    )
    return {
        "definition": INVENTORY_DEFINITION,
        "pairs": len(pairs),
        "distinct_hashes": len({h for _, h in pairs}),
        "files": len({p for p, _ in pairs}),
        "paths": sorted({safe_path(p) for p, _ in pairs}),
        "existing": existing,
        "missing": len({h for _, h in pairs}) - existing,
        "rev_parse": calls,
        "rev_parse_files": len({r["path"] for r in calls}),
        "rev_parse_calls": len(calls),
    }


def _run_audit(root: Path) -> dict:
    root = root.resolve()
    tracked = sorted(git(root, "ls-files", "-z").decode().rstrip("\0").split("\0"))
    head = git(root, "log", "-1", "--format=%H").decode().strip()
    objects = git(root, "rev-list", "--objects", "--all").decode().splitlines()
    history_paths = {
        p.lstrip("\n")
        for p in git(root, "log", "--all", "--name-only", "--format=", "-z")
        .decode()
        .split("\0")
        if p.lstrip("\n")
    }
    reference, sources = load_sources(root)
    names = (
        load_banned_names(root)
        if (root / "data/seed/banned_names.yaml").is_file()
        else ()
    )

    def finding(text, path):
        value = scan_text(text, reference)
        if not path.startswith(("data/manifest/", "data/interim/")):
            value["patterns"].pop("personal_field", None)
        if path.startswith("data/synthetic/"):
            count = len(find_banned_names(text, names))
            if count:
                value["patterns"]["real_institution"] = count
        return {"path": safe_path(path), **value}

    def empty():
        return {
            "scanned_files": 0,
            "scanned_blobs": 0,
            "oversized": 0,
            "undecodable": 0,
            "excluded_sensitive_worktree": 0,
            "findings": [],
        }

    working = empty()
    for path in tracked:
        if not path:
            continue
        if (
            "env" in path_kinds(path)
            or path.startswith(("data/raw/", "data/interim/"))
            or Path(path).name == "tokens.json"
        ):
            working["excluded_sensitive_worktree"] += 1
            continue
        data = read(root / path)
        if len(data) > MAX_BLOB:
            working["oversized"] += 1
            continue
        text = decode(data)
        working["scanned_files"] += 1
        if text is None:
            working["undecodable"] += 1
        else:
            value = finding(text, path)
            if value["patterns"] or value["overlap_shingles"]:
                working["findings"].append(value)
    history = empty()
    unique = {}
    for obj in objects:
        sha, _, path = obj.partition(" ")
        unique.setdefault(sha, path)
    metadata = (
        git(
            root,
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            input_data=("\n".join(unique) + "\n").encode(),
        )
        .decode()
        .splitlines()
    )
    blobs = []
    for row in metadata:
        sha, kind, size = row.split()
        if kind == "blob":
            if int(size) > MAX_BLOB:
                history["oversized"] += 1
            else:
                blobs.append(sha)
    for start in range(0, len(blobs), 32):
        batch = blobs[start : start + 32]
        raw = git(
            root, "cat-file", "--batch", input_data=("\n".join(batch) + "\n").encode()
        )
        offset = 0
        for sha in batch:
            end = raw.index(b"\n", offset)
            size = int(raw[offset:end].split()[-1])
            data = raw[end + 1 : end + 1 + size]
            offset = end + 2 + size
            history["scanned_blobs"] += 1
            text = decode(data)
            if text is None:
                history["undecodable"] += 1
            else:
                value = finding(text, unique[sha])
                if value["patterns"] or value["overlap_shingles"]:
                    history["findings"].append(value)
    if not history["scanned_blobs"]:
        raise AuditError("스캔한 이력 blob이 0개입니다")
    refs = Counter()
    for ref in git(root, "for-each-ref", "--format=%(refname)").decode().splitlines():
        refs[ref.split("/")[1] if "/" in ref else "other"] += 1
    return {
        "executed_at": datetime.now(UTC).isoformat(),
        "head_commit": head,
        "worktree": working,
        "history": history,
        "paths": {
            "current": path_inventory(set(filter(None, tracked))),
            "history": path_inventory(history_paths),
        },
        "email_domains": email_inventory(root),
        "shingles": sources,
        "commit_inventory": commit_inventory(root, tracked),
        "hygiene": {
            p: (root / p).is_file() for p in ("LICENSE", "README.md", ".env.example")
        }
        | {"env_in_history": any("env" in path_kinds(p) for p in history_paths)},
        "refs": dict(sorted(refs.items())),
        "exclusions": {
            "max_blob_bytes": MAX_BLOB,
            "worktree_sensitive_reason": ".env·로컬 원문·평문 토큰 파일은 직접 열지 않음; 이력 blob은 점검",
            "binary_reason": "UTF-8 디코드 불가 또는 NUL 포함",
        },
    }


def run_audit(root: Path) -> dict:
    try:
        return _run_audit(root)
    except Exception:  # noqa: BLE001 -- sanitize even malformed local YAML and git metadata
        raise AuditError("로컬 점검 입력 또는 git 오류") from None


DECISIONS = """## 사용자 결정 (사용자 보존 영역)

- [ ] 공개 여부(사용자 결정). 결정: 
  - 근거: 생성 영역 전체, [위협 모델](../THREAT_MODEL.md).
- [ ] 이력 처리. 결정: 
  - **기본안: 이력 유지.** 결과 문서·manifest의 커밋 참조(m5_config.code_commit, M7 측정 코드 커밋, rev-parse 기록 코드)가 이력 재작성 후 없는 커밋을 가리키고, 결과를 보기 전에 규칙을 커밋했다는 시간순 근거가 깨진다. 새 저장소(스쿼시)도 같은 이유로 제외한다. 근거: [ADR-026](../ADR.md), 생성 영역 commit_inventory.
  - 이력 정리 대안(`filter-repo --mailmap` + old→new SHA 대응표 + 문서 내 해시 갱신)의 비용: 회사 메일을 반드시 지워야 하는 경우만 고려. 대상 파일 수·해시 수·rev-parse 위치는 생성 영역 commit_inventory의 files·pairs·distinct_hashes·rev_parse_files·rev_parse_calls 참조. 실행 절차나 스크립트는 만들지 않는다.
- [ ] 작성자 이메일 확인. 결정: 
  - 사용자 설명: liveuou.kr 21건은 GitHub 웹 머지 커밋(계정 이메일), 회사 메일 도메인은 5건. 스캐너의 생성 영역 email_domains와 대조: 
  - [ ] 앞으로의 커밋 이메일(noreply)을 사용자가 GitHub에서 직접 설정.
- [ ] LICENSE. 결정: 
  - 코드의 기본 후보 MIT. 범위: "합성 데이터·문서는 같은 라이선스, 실제 규정 원문은 저장소에 없음". 공개 직전 사용자 최종 결정. 근거: [ADR-002](../ADR.md), 생성 영역 hygiene·paths·shingles.
- [ ] 슁글 겹침 검토. 결정: 
  - 생성 영역 worktree·history findings의 건수·최대 길이가 큰 파일을 직접 검토한다. 법령 상투구 오탐 가능성이 있어 스캐너는 판정하지 않는다. 원문 없으면 건너뜀이며 통과가 아니다.
- [ ] GitHub 이슈·PR·댓글 점검 결과(`make m8-audit-gh`) 확인. 결정: 
  - 근거: 생성 영역 github 및 data/manifest/m8_audit_gh.json. 공개하면 refs/pull/*도 함께 보인다는 점 확인(로컬 refs에는 없을 수 있음).
- [ ] 공개 직전 `make m8-audit` 재실행. 결정: 
  - 이후 커밋도 점검 대상에 들어간다. 근거: 생성 영역 executed_at·head_commit. manifest와 생성 영역은 재실행마다 덮어쓴다.
"""


def write_outputs(root: Path, result: dict) -> None:
    try:
        (root / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
        (root / MANIFEST).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        gh_path = root / "data/manifest/m8_audit_gh.json"
        gh = (
            json.loads(read(gh_path))
            if gh_path.is_file()
            else {"status": "미실행: 사용자 실행 필요"}
        )
        body = (
            "## 결정론 점검 집계\n\n발견 건수는 공개 판정이 아니다. 패턴은 참고 위치이며 매치 값은 기록하지 않는다.\n\n```json\n"
            + json.dumps({**result, "github": gh}, ensure_ascii=False, indent=2)
            + "\n```"
        )
        path = root / REPORT
        document = (
            read(path).decode()
            if path.is_file()
            else f"# M8 공개 전환 점검\n\n{BEGIN}\n{END}\n\n{DECISIONS}"
        )
        if (
            document.count(BEGIN) != 1
            or document.count(END) != 1
            or document.index(BEGIN) >= document.index(END)
        ):
            raise AuditError("결과 문서 생성 영역 형식 오류")
        before, rest = document.split(BEGIN)
        _, after = rest.split(END)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            before + BEGIN + "\n" + body + "\n" + END + after, encoding="utf-8"
        )
    except (OSError, ValueError, UnicodeError):
        raise AuditError("결과 저장 오류") from None


def main(*, root: Path = Path(".")) -> int:
    try:
        result = run_audit(root)
        write_outputs(root, result)
    except Exception:  # noqa: BLE001 -- final value-free boundary, never log exceptions
        # No underlying exception or traceback can leak git stderr or input bytes.
        print("M8 로컬 점검 도구 오류", file=sys.stderr)
        return 1
    totals = Counter()
    for area in ("worktree", "history"):
        for item in result[area]["findings"]:
            totals.update(item["patterns"])
    print(
        json.dumps(
            {
                "pattern_counts": dict(sorted(totals.items())),
                "scanned_blobs": result["history"]["scanned_blobs"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
