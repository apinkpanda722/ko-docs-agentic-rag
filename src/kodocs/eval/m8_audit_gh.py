"""Operator-only GitHub GET inventory; bodies never leave the scanner."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from kodocs.eval.m8_audit import (
    MANIFEST as LOCAL_MANIFEST,
)
from kodocs.eval.m8_audit import (
    AuditError,
    git,
    load_sources,
    read,
    scan_text,
    write_outputs,
)

MANIFEST = Path("data/manifest/m8_audit_gh.json")


def repository(root: Path) -> str:
    remote = git(root, "remote", "get-url", "origin").decode().strip()
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
        remote,
    )
    if not match:
        raise AuditError("GitHub origin 형식 오류")
    return match[1]


def get_page(endpoint: str) -> list:
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--method", "GET"], capture_output=True, check=False
        )
        if result.returncode:
            raise AuditError("GitHub GET 오류")
        payload = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise AuditError("GitHub 응답 형식 오류")
        return payload
    except (OSError, ValueError, UnicodeError):
        raise AuditError("GitHub 읽기 오류") from None


def run_audit(root: Path) -> dict:
    repo = repository(root)
    reference, sources = load_sources(root)
    output = {
        "executed_at": datetime.now(UTC).isoformat(),
        "counts": {
            "issues": 0,
            "prs": 0,
            "issue_comments": 0,
            "review_comments": 0,
            "reviews": 0,
        },
        "item_failures": 0,
        "page_failures": 0,
        "findings": [],
        "shingles": sources,
        "pull_refs_basis": "PR 개수는 공개 시 refs/pull/* 노출 근거이며 실제 ref 수와 동일하다는 판정은 하지 않음",
    }

    def pages(endpoint):
        page = 1
        while True:
            try:
                items = get_page(
                    f"repos/{repo}/{endpoint}?state=all&per_page=100&page={page}"
                )
            except AuditError:
                output["page_failures"] += 1
                return
            if not items:
                return
            yield from items
            page += 1

    def inspect(item, kind, number=None):
        try:
            if not isinstance(item, dict):
                raise TypeError
            number = item.get("number", item.get("id")) if number is None else number
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise TypeError
            body = item.get("body")
            if body is None:
                body = ""
            if not isinstance(body, str):
                raise TypeError
            output["counts"][kind] += 1
            result = scan_text(body, reference)
            if result["patterns"] or result["overlap_shingles"]:
                output["findings"].append({"kind": kind, "number": number, **result})
            return number
        except (ValueError, TypeError):
            output["item_failures"] += 1
            return None

    for item in pages("issues"):
        if isinstance(item, dict) and "pull_request" in item:
            continue
        inspect(item, "issues")
    for item in pages("pulls"):
        number = inspect(item, "prs")
        if number is None:
            continue
        before = output["page_failures"]
        for review in pages(f"pulls/{number}/reviews"):
            inspect(review, "reviews", number)
        output["item_failures"] += output["page_failures"] - before
    for kind, endpoint in [
        ("issue_comments", "issues/comments"),
        ("review_comments", "pulls/comments"),
    ]:
        for item in pages(endpoint):
            # Comment IDs are locations; parent issue/PR number is also retained.
            inspect(item, kind)
    if output["page_failures"] and not any(output["counts"].values()):
        raise AuditError("GitHub 입력을 읽지 못했습니다")
    output["counts"]["issues_and_prs"] = (
        output["counts"]["issues"] + output["counts"]["prs"]
    )
    return output


def main(*, root: Path = Path(".")) -> int:
    try:
        result = run_audit(root)
        target = root / MANIFEST
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if (root / LOCAL_MANIFEST).is_file():
            write_outputs(root, json.loads(read(root / LOCAL_MANIFEST)))
    except Exception:  # noqa: BLE001 -- do not disclose gh stderr or response bodies
        print("M8 GitHub 점검 도구 오류", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "counts": result["counts"],
                "item_failures": result["item_failures"],
                "page_failures": result["page_failures"],
            },
            ensure_ascii=False,
        )
    )
    return int(result["page_failures"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
