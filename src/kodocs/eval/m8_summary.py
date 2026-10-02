"""Sentences summarizing the H8 result, generated from manifests only.

The adopted verdict must never stand alone: it is an average over 21 gold
questions, so the source split, the harm count, the candidate ceiling and the
latency caveat are always printed next to it. Every number comes from the
verified result/latency/dev manifests; missing optional parts are skipped.
"""

import json
from pathlib import Path

from kodocs.eval.m7_report import ci3, f3


def _get(value, *path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _range(low, high):
    return "전부 0" if low == high == 0 else f"{f3(low, True)}~{f3(high, True)}"


def source_split_lines(result):
    """Real vs synthetic gold questions (descriptive, not a verdict)."""
    split = _get(result, "auxiliary", "by_source", "gold") or {}
    lines = []
    real, synthetic = split.get("real"), split.get("synthetic")
    if real:
        diff = real["ndcg_diff"]
        lines.append(
            f"- 실제 규정 {real['n']}문항: 대조군 NDCG@10 {f3(real['control']['ndcg10'])}"
            f" → 재정렬 {f3(real['reranked']['ndcg10'])}, 문항별 차이 {_range(diff['min'], diff['max'])}"
            f"(평균 {f3(diff['mean'], True)}). 대조군이 이미 높아 올릴 여지가 작은 천장 구간이다."
        )
    if synthetic:
        diff = synthetic["ndcg_diff"]
        lines.append(
            f"- 합성 {synthetic['n']}문항: 대조군 NDCG@10 {f3(synthetic['control']['ndcg10'])}"
            f" → 재정렬 {f3(synthetic['reranked']['ndcg10'])}, 평균 차이 {f3(diff['mean'], True)}"
            f"(문항별 {f3(diff['min'], True)}~{f3(diff['max'], True)})."
        )
    if real and synthetic:
        lines.append(
            "- 따라서 채택된 평균 이득은 합성 문항에서 나왔다. 출처별 분리는 기술 통계이며 판정이 아니다."
        )
    return lines


def summary_lines(result, latency=None):
    """Bullet lines for the key summary / README (empty pieces are skipped)."""
    h8 = result["h8"]
    lines = [
        f"- 판정 “{h8['verdict']}”은 골드 test {h8['n']}문항 평균이며, 아래 항목과 함께 읽어야 한다."
    ]
    lines += source_split_lines(result)
    silver = _get(result, "comparisons", "silver", "ndcg10")
    if silver and silver.get("mean_diff") is not None:
        lines.append(
            f"- 실버 방향 확인(판정 아님, n={silver['n']}): 재정렬 − 대조군 NDCG@10 "
            f"{f3(silver['mean_diff'], True)} {ci3(silver['ci_low'], silver['ci_high'])}."
        )
    harm = result.get("harm")
    if harm:
        lines.append(
            f"- 순위가 나빠진 문항(문항별 NDCG 차이 < 0): 골드 {harm['gold']}, 실버 {harm['silver']}."
        )
    recall50 = {s: _get(result, "auxiliary", s, "recall50") for s in ("gold", "silver")}
    if all(v is not None for v in recall50.values()):
        lines.append(
            f"- 후보 50의 문서 단위 Recall@50(재정렬이 올릴 수 있는 상한): 골드 {f3(recall50['gold'])},"
            f" 실버 {f3(recall50['silver'])}. 재정렬 후 Recall@10은 골드 {f3(result['gold']['reranked']['recall10'])},"
            f" 실버 {f3(result['silver']['reranked']['recall10'])}."
        )
    if latency:
        lines.append(latency_line(latency))
    lines.append(
        "- 검색 지표만 측정했다. 답변 수준 효과는 측정하지 않았고 리랭커는 평가 경로 전용이다(`search_docs`·MCP에 연결하지 않았다)."
    )
    return lines


def latency_line(latency):
    rerank = latency["rerank_s"]
    return (
        f"- 지연: 재정렬 구간 P95 {f3(rerank['p95_s'])}초를 M7 단순 RAG P95 {f3(latency['simple_p95_m7'])}초에 더한"
        f" 보수적 합 {f3(latency['conservative_sum_s'])}초로, 5.0초 목표 대비 여유 {f3(latency['margin_to_5s'], True)}초다"
        f"(목표를 넘는 수준). 단서: 두 P95를 단순히 더한 근사이며 분포를 합성한 값이 아니고, M7 값은 임베딩 캐시 적중 상태에서"
        f" 쟀다. 판정이 아니다."
    )


def improvement_bullet(root, result, selected):
    """README "next improvement" line, numbers from the dev manifest."""
    path = Path(root) / "data/manifest/m8_dev.json"
    if not path.exists() or not result:
        return None
    grid = json.loads(path.read_text(encoding="utf-8")).get("grid", [])
    fusion = "alone" if selected["fusion"] in {"단독", "alone"} else "rrf"
    rows = {r["depth"]: r for r in grid if r["fusion"] == fusion}
    chosen, smallest = selected["depth"], min(rows, default=None)
    if smallest is None or smallest >= chosen or chosen not in rows:
        return None
    return (
        "- 리랭커 서빙 통합: `search_docs` v2 계약, depth 축소"
        f"(dev depth {smallest} 골드 {f3(rows[smallest]['gold_dev_mean_ndcg10'])} 대 {chosen}의"
        f" {f3(rows[chosen]['gold_dev_mean_ndcg10'])}, 채점량 {smallest / chosen:.0%}) 또는 GPU로 지연 목표 충족 후"
        " 답변 수준 재평가. 현재는 평가 경로 전용이다."
    )
