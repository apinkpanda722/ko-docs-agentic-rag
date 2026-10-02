"""Final-only streaming and queue transport; no Streamlit calls in workers."""

import json
import queue
import threading

from langchain_core.utils.json import parse_partial_json


class FinalStreamingChat:
    """Follow M7 StreamingChat, attaching the UI observer only to final calls."""

    def __init__(self, inner, on_text_delta, on_attempt_start=None):
        self.inner = inner
        self.on_text_delta = on_text_delta
        self.on_attempt_start = on_attempt_start

    def invoke(
        self,
        messages,
        *,
        tools=None,
        response_schema=None,
        stream=False,
        on_text_delta=None,
        on_attempt_start=None,
    ):
        kwargs = {}
        if tools is not None:
            kwargs["tools"] = tools
        if response_schema is not None:
            kwargs.update(
                response_schema=response_schema,
                stream=True,
                on_text_delta=self.on_text_delta,
            )
            if self.on_attempt_start is not None:
                kwargs["on_attempt_start"] = self.on_attempt_start
        return self.inner.invoke(messages, **kwargs)


class PartialText:
    """Accumulated streamed JSON of the current attempt only."""

    def __init__(self):
        self._parts = []

    def add(self, delta):
        self._parts.append(delta)

    def reset(self):
        """A new attempt started: drop text from earlier attempts."""
        self._parts.clear()

    def answer(self):
        return partial_answer("".join(self._parts))


def partial_answer(text):
    """Use langchain_core/utils/json.py:58; it handles unfinished escapes."""
    try:
        parsed = parse_partial_json(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    value = parsed.get("answer") if isinstance(parsed, dict) else None
    return value if isinstance(value, str) else ""


def run_worker(work, render):
    """Run one answer; render events exclusively on the calling script thread.

    ToolNode uses executor.map (langgraph/prebuilt/tool_node.py:821-823).
    Streamlit ScriptRunContext (script_run_context.py:201-212) is thread-bound.
    Workers have no attached context and only put onto this shared Queue.
    """
    messages = queue.Queue()
    done = object()
    outcome = []

    def worker():
        try:
            outcome.append((True, work(messages)))
        except BaseException as exc:  # noqa: BLE001 - deliver errors and interruption to caller
            outcome.append((False, exc))
        finally:
            messages.put(done)

    thread = threading.Thread(target=worker, name="kodocs-ui-answer")
    thread.start()
    try:
        while True:
            item = messages.get()
            if item is done:
                break
            render(*item)
    finally:
        thread.join()
    success, value = outcome[0]
    if not success:
        raise value
    return value


def event_lines(events):
    """Current status per invocation, including simultaneous parallel tools."""
    states = {}
    for event in events:
        if event["kind"].startswith("tool_"):
            states[event["index"]] = event
    lines = []
    for event in states.values():
        if event["kind"] == "tool_start":
            state = "실행 중"
        elif event["error"]:
            state = f"오류({event['error']})"
        else:
            state = f"완료({event['latency_ms']:.1f} ms, refs {event['refs_count']})"
        lines.append(f"{event['name']} · {state}")
    return lines
