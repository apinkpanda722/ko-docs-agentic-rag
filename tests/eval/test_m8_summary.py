"""The adopted verdict never stands alone; every number comes from manifests."""

import json
from pathlib import Path

from kodocs.eval import m8_summary as s

ROOT = Path(__file__).resolve().parents[2]


def result(real_diffs=(0.0, 0.0), synthetic_mean=0.169):
    def group(n, control, reranked, low, high, mean):
        return {
            "n": n,
            "control": {"ndcg10": control},
            "reranked": {"ndcg10": reranked},
            "ndcg_diff": {"min": low, "max": high, "mean": mean},
        }

    return {
        "h8": {"verdict": "H8 채택", "n": 21},
        "gold": {"reranked": {"recall10": 0.796}},
        "silver": {"reranked": {"recall10": 0.544}},
        "harm": {"gold": 0, "silver": 12},
        "comparisons": {
            "silver": {
                "ndcg10": {
                    "n": 65,
                    "mean_diff": 0.107,
                    "ci_low": 0.061,
                    "ci_high": 0.157,
                }
            }
        },
        "auxiliary": {
            "gold": {"recall50": 0.849},
            "silver": {"recall50": 0.725},
            "by_source": {
                "gold": {
                    "real": group(13, 0.942, 0.942, *real_diffs, 0.0),
                    "synthetic": group(8, 0.354, 0.522, 0.007, 0.369, synthetic_mean),
                }
            },
        },
    }


def latency():
    return {
        "rerank_s": {"p95_s": 3.264},
        "simple_p95_m7": 3.732,
        "conservative_sum_s": 6.996,
        "margin_to_5s": -1.996,
    }


def test_verdict_comes_with_source_split_harm_ceiling_and_latency_caveat():
    text = "\n".join(s.summary_lines(result(), latency()))
    assert "“H8 채택”은 골드 test 21문항 평균이며, 아래 항목과 함께" in text
    assert "실제 규정 13문항" in text and "문항별 차이 전부 0" in text
    assert "합성 8문항" in text and "+0.169" in text
    assert "채택된 평균 이득은 합성 문항에서 나왔다" in text
    assert "골드 0, 실버 12" in text
    assert "Recall@50" in text and "0.849" in text and "0.725" in text
    assert "6.996초" in text and "-1.996" in text
    assert "단순히 더한 근사" in text and "분포를 합성한 값이 아니고" in text
    assert "임베딩 캐시 적중" in text and "판정이 아니다" in text
    assert "답변 수준 효과는 측정하지 않았고" in text


def test_nonzero_real_differences_are_not_called_all_zero():
    text = "\n".join(s.source_split_lines(result(real_diffs=(-0.01, 0.0))))
    assert "전부 0" not in text and "-0.010~0.000" in text


def test_a_result_without_optional_manifests_skips_those_lines():
    bare = {"h8": {"verdict": "H8 채택", "n": 21}}
    lines = s.summary_lines(bare)
    assert len(lines) == 2  # verdict note + the scope sentence
    assert not any("지연" in line for line in lines)


def test_source_split_needs_both_sources_for_the_conclusion():
    one = result()
    del one["auxiliary"]["by_source"]["gold"]["synthetic"]
    assert "합성에서 나왔다" not in "\n".join(s.source_split_lines(one))


def test_improvement_bullet_uses_the_dev_manifest_numbers():
    selected = {"depth": 50, "fusion": "단독"}
    bullet = s.improvement_bullet(ROOT, {"h8": {}}, selected)
    dev = json.loads((ROOT / "data/manifest/m8_dev.json").read_text())["grid"]
    rows = {r["depth"]: r for r in dev if r["fusion"] == "alone"}
    assert f"{rows[20]['gold_dev_mean_ndcg10']:.3f}" in bullet
    assert f"{rows[50]['gold_dev_mean_ndcg10']:.3f}" in bullet
    assert "채점량 40%" in bullet and "`search_docs` v2 계약" in bullet
    assert "현재는 평가 경로 전용" in bullet
    assert s.improvement_bullet(ROOT, None, selected) is None  # no result: no claim
    assert (
        s.improvement_bullet(ROOT, {"h8": {}}, {"depth": 20, "fusion": "단독"}) is None
    )


def test_serving_follow_up_is_readme_only_not_in_the_gui():
    for path in (ROOT / "src/kodocs/ui").glob("*.py"):
        assert "서빙 통합" not in path.read_text(encoding="utf-8"), path
