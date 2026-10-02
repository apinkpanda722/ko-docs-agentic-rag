"""Korean demonstration views; rendering alone never calls the answer service."""

from pathlib import Path

import streamlit as st

from kodocs.ui.service import UiAnswer, UiError, ask
from kodocs.ui.users import load_demo_users

RESULTS_DIR = Path("docs/results")


def render_answer(label: str, result: UiAnswer | str) -> None:
    st.subheader(label)
    if isinstance(result, str):
        st.error(result)
        return
    st.markdown(result.answer)
    # Refusals show exactly the system answer; internal status stays in the trace.
    if not result.refused and result.citations:
        st.markdown("**인용**")
        for citation in result.citations:
            if citation.doc_id:
                st.markdown(
                    " · ".join(
                        value
                        for value in (
                            citation.doc_id,
                            citation.section_key,
                            citation.title,
                        )
                        if value
                    )
                )
            else:
                st.markdown(f"SQL 실행 근거: {citation.call_id}")
    st.caption(
        f"비용 {result.cost_usd:.6f} USD · 모델 {result.model} · 지연 {result.total_ms / 1000:.3f}초"
    )
    with st.expander("도구 호출 트레이스"):
        st.write({"거절 여부": result.refused, "내부 종료 코드": result.refuse_reason})
        for tool in result.tools:
            st.markdown(f"**{tool.name}**")
            st.text(f"인자: {tool.args_summary}")
            st.text(f"지연: {tool.latency_ms:.3f} ms · 토큰: {tool.tokens}")


def query_view() -> None:
    try:
        users = load_demo_users()
    except (ValueError, TypeError) as exc:
        st.error(str(exc))
        return
    question = st.text_area("질문", key="question")
    system = st.selectbox("시스템", ["agentic", "simple"], key="system")
    labels = [user.label for user in users]
    a = st.selectbox("사용자 A", labels, key="user_a")
    b = st.selectbox("사용자 B", labels, index=min(1, len(labels) - 1), key="user_b")
    single = st.button("사용자 A로 실행", key="single")
    compare = st.button("같은 질문을 두 사용자로 실행", key="compare")
    if single or compare:
        if not question.strip():
            st.warning("질문을 입력해 주세요.")
        else:
            selected = [a, b] if compare else [a]
            by_label = {user.label: user for user in users}
            answers = []
            for label in selected:
                try:
                    with st.spinner(f"{label} 답변 생성 중"):
                        result = ask(question, by_label[label], system)
                    st.session_state.session_cost_usd += result.cost_usd
                except UiError as exc:
                    result = str(exc)
                answers.append((label, result))
            st.session_state.answers = answers
    answers = st.session_state.get("answers", [])
    if answers:
        for column, (label, result) in zip(
            st.columns(len(answers)), answers, strict=True
        ):
            with column:
                render_answer(label, result)


def results_view() -> None:
    include_appendix = st.checkbox("부록 포함", key="include_appendix")
    paths = {
        path.name: path
        for path in sorted(RESULTS_DIR.glob("*.md"))
        if (include_appendix or not path.name.endswith("-appendix.md"))
        and path.resolve().parent == RESULTS_DIR.resolve()
    }
    if not paths:
        st.info("표시할 결과 문서가 없습니다.")
        return
    selection = st.selectbox("결과 문서", list(paths), key="report")
    try:
        st.markdown(paths[selection].read_text(encoding="utf-8"))
    except OSError:
        st.error("결과 문서를 읽을 수 없습니다.")


def main() -> None:
    st.set_page_config(page_title="한국어 문서 RAG 데모", layout="wide")
    st.title("한국어 사내 문서 RAG 데모")
    if "session_cost_usd" not in st.session_state:
        st.session_state.session_cost_usd = 0.0
    cost = st.empty()
    query_tab, results_tab = st.tabs(["질의", "결과표"])
    with query_tab:
        query_view()
    with results_tab:
        results_view()
    cost.metric("세션 누적 비용", f"{st.session_state.session_cost_usd:.6f} USD")
