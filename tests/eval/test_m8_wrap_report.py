import pytest

from kodocs.eval import m8_wrap_report as wrap


def test_package_table_reads_fixture_values(tmp_path):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="sentence-transformers"\nversion="fixture1"\n[[package]]\nname="httpx"\nversion="fixture2"\n'
    )
    for path, text in [
        (
            "src/kodocs/retrieval/embedding.py",
            "from sentence_transformers import SentenceTransformer\n",
        ),
        ("src/kodocs/retrieval/opensearch.py", "import httpx\n"),
    ]:
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    rows = wrap.package_rows(
        tmp_path,
        {"environment": {"packages": {"FlagEmbedding": None, "opensearch-py": "old"}}},
    )
    assert rows[0][1] == "null" and "fixture1" in rows[0][2]
    assert rows[1][1] == "old" and "fixture2" in rows[1][2]
    assert "embedding.py:1" in rows[0][3]


def test_annex_manifest_counts_and_unique_questions():
    audit = {
        "sections_compared": 7,
        "below_70_percent": 3,
        "unit_label_lost_sections": 2,
        "unit_label_lost_regulations": 10,
    }
    questions = {
        "gold": [
            {
                "id": "q",
                "split": "dev",
                "qrels": [{"doc_id": wrap.REGULATION_IDS[0], "annex_id": "1"}] * 2,
            }
        ],
        "silver": [],
    }
    body = wrap.annex_section(audit, questions, wrap.REGULATION_IDS)
    assert "7" in body and "3" in body and "2" in body
    assert "| gold | dev | 1 |" in body
    assert "| gold | test | 0 |" in body
    assert "규정 단위 상한" in body and "미산출" in body
    assert wrap.CONCLUSION in body
    with pytest.raises(ValueError):
        wrap.annex_section(
            {**audit, "unit_label_lost_regulations": 9}, questions, wrap.REGULATION_IDS
        )


def test_regulation_list_from_document_and_count():
    ids = "law-2200000148327·148341·148345·148727·148765·148769·149621·150485·150587·151395"
    assert (
        wrap.regulations("## M8 개선 후보(사용자 보존 영역)\n10개 규정: " + ids + ")")
        == wrap.REGULATION_IDS
    )
    with pytest.raises(ValueError):
        wrap.regulations("## M8 개선 후보\n9개 규정: " + ids + ")")


def test_preserved_region_and_invalid_markers(tmp_path, monkeypatch):
    path = tmp_path / wrap.REPORT
    path.parent.mkdir(parents=True)
    original = (
        "# Title\n"
        + wrap.BEGIN
        + "\nold\n"
        + wrap.END
        + "\n## 사용자 보존 영역\nUSER\n"
    )
    path.write_text(original)
    monkeypatch.setattr(wrap, "render", lambda root: "new")
    wrap.write_report(tmp_path)
    assert path.read_text().endswith(wrap.END + "\n## 사용자 보존 영역\nUSER\n")
    assert path.read_text().startswith("# Title\n" + wrap.BEGIN)
    path.write_text(wrap.END + wrap.BEGIN)
    with pytest.raises(ValueError):
        wrap.write_report(tmp_path)


def test_preserved_bytes_include_crlf(tmp_path, monkeypatch):
    path = tmp_path / wrap.REPORT
    path.parent.mkdir(parents=True)
    suffix = (wrap.END + "\r\n## 사용자 보존 영역\r\nUSER\r\n").encode()
    path.write_bytes(b"# Title\r\n" + wrap.BEGIN.encode() + b"\r\nold\r\n" + suffix)
    monkeypatch.setattr(wrap, "render", lambda root: "new")
    wrap.write_report(tmp_path)
    assert path.read_bytes().endswith(suffix)
