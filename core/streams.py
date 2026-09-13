"""流式转换状态机：上游 chat.completion.chunk SSE → 下游 Responses / Anthropic 事件流。

用法：feed(chunk_text) 逐块喂入（内部按空行切分 SSE 事件），emit(event, data) 回调
产出下游事件；上游发 [DONE] 或连接结束后调用 finalize()。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .util import rand_id


class _Base:
    def __init__(self) -> None:
        self.buffer = ""
        self.usage: dict | None = None
        self.error: str | None = None

    def feed(self, chunk: str) -> None:
        self.buffer += chunk.replace("\r\n", "\n")
        while True:
            pos = self.buffer.find("\n\n")
            if pos < 0:
                return
            raw, self.buffer = self.buffer[:pos], self.buffer[pos + 2 :]
            self._handle_raw(raw)

    def _handle_raw(self, raw: str) -> None:
        data = ""
        for line in raw.split("\n"):
            if line.startswith("data:"):
                data += line[5:].lstrip()
        if not data:
            return
        if data == "[DONE]":
            self.finalize()
            return
        try:
            chunk = json.loads(data)
        except Exception:
            return
        if isinstance(chunk, dict):
            self._handle_chunk(chunk)


class ResponsesStream(_Base):
    """chat chunk → Responses 事件流。"""

    def __init__(self, emit: Callable[[str, dict], None], model: str = "") -> None:
        super().__init__()
        self.emit = emit
        self.model = model
        self.resp_id = rand_id("resp_")
        self.rs_id = rand_id("rs_")
        self.msg_id = rand_id("msg_")
        self.created = 0
        self.started = False
        self.done = False
        self.finish: str | None = None
        self.rs_open = False
        self.rs_oi = 0
        self.rs_acc = ""
        self.part_open = False
        self.msg_oi = 0
        self.text_acc = ""
        self.tools: dict[int, dict] = {}
        self.next_oi = 0

    def _handle_chunk(self, c: dict) -> None:
        if not self.started:
            if not self.model:
                self.model = str(c.get("model") or "")
            self.created = int(c.get("created") or time.time())
            self._start()
        if isinstance(c.get("usage"), dict):
            self.usage = c["usage"]
        if c.get("error"):
            self.done = True
            resp = self._skeleton("failed", True)
            resp["error"] = {
                "code": "upstream_error",
                "message": str((c.get("error") or {}).get("message") or "upstream error"),
            }
            self.emit("response.failed", {"response": resp})
            return
        choice = (c.get("choices") or [None])[0]
        if not isinstance(choice, dict):
            return
        if choice.get("finish_reason"):
            self.finish = str(choice["finish_reason"])
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            return
        self._reasoning(delta)
        self._content(delta)
        self._tools(delta)

    def _start(self) -> None:
        self.started = True
        resp = self._skeleton("in_progress")
        self.emit("response.created", {"response": resp})
        self.emit("response.in_progress", {"response": resp})

    def _skeleton(self, status: str, with_output: bool = False) -> dict:
        return {
            "id": self.resp_id,
            "object": "response",
            "created_at": self.created or int(time.time()),
            "status": status,
            "error": None,
            "incomplete_details": (
                {"reason": "content_filter" if self.finish == "content_filter" else "max_output_tokens"}
                if status == "incomplete"
                else None
            ),
            "model": self.model,
            "output": self._build_output() if with_output else [],
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "temperature": None,
            "top_p": None,
            "max_output_tokens": None,
            "tools": [],
            "tool_choice": "auto",
            "usage": self._usage(),
            "metadata": {},
        }

    def _usage(self) -> dict:
        u = self.usage or {}
        tin, tout = u.get("prompt_tokens"), u.get("completion_tokens")
        return {
            "input_tokens": tin,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": tout,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": u.get("total_tokens") or (((tin or 0) + (tout or 0)) or None),
        }

    def _build_output(self) -> list:
        out: list = []
        if self.rs_acc:
            out.append(
                {
                    "id": self.rs_id,
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": self.rs_acc}],
                }
            )
        if self.text_acc or not self.tools:
            out.append(
                {
                    "id": self.msg_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self.text_acc, "annotations": []}],
                }
            )
        for t in self.tools.values():
            out.append(self._tool_item(t, "completed"))
        return out

    def _tool_item(self, t: dict, status: str) -> dict:
        return {
            "id": t["id"],
            "type": "function_call",
            "status": status,
            "call_id": t["call_id"],
            "name": t["name"],
            "arguments": t["args"],
        }

    def _final_status(self) -> str:
        return "incomplete" if self.finish in ("length", "content_filter") else "completed"

    def _reasoning(self, delta: dict) -> None:
        rs = delta.get("reasoning_content") or delta.get("reasoning")
        if not isinstance(rs, str) or not rs:
            return
        if not self.rs_open:
            self.rs_open = True
            self.rs_oi = self.next_oi
            self.next_oi += 1
            self.emit(
                "response.output_item.added",
                {"output_index": self.rs_oi, "item": {"id": self.rs_id, "type": "reasoning", "summary": []}},
            )
            self.emit(
                "response.reasoning_summary_part.added",
                {
                    "item_id": self.rs_id,
                    "output_index": self.rs_oi,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
            )
        self.rs_acc += rs
        self.emit(
            "response.reasoning_summary_text.delta",
            {"item_id": self.rs_id, "output_index": self.rs_oi, "summary_index": 0, "delta": rs},
        )

    def _close_reasoning(self) -> None:
        self.emit(
            "response.reasoning_summary_text.done",
            {"item_id": self.rs_id, "output_index": self.rs_oi, "summary_index": 0, "text": self.rs_acc},
        )
        self.emit(
            "response.output_item.done",
            {
                "output_index": self.rs_oi,
                "item": {
                    "id": self.rs_id,
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": self.rs_acc}],
                },
            },
        )
        self.rs_open = False

    def _content(self, delta: dict) -> None:
        content = delta.get("content")
        if not isinstance(content, str) or not content:
            return
        if self.rs_open:
            self._close_reasoning()
        if not self.part_open:
            self.part_open = True
            self.msg_oi = self.next_oi
            self.next_oi += 1
            self.emit(
                "response.output_item.added",
                {
                    "output_index": self.msg_oi,
                    "item": {
                        "id": self.msg_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            )
            self.emit(
                "response.content_part.added",
                {
                    "item_id": self.msg_id,
                    "output_index": self.msg_oi,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            )
        self.text_acc += content
        self.emit(
            "response.output_text.delta",
            {"item_id": self.msg_id, "output_index": self.msg_oi, "content_index": 0, "delta": content},
        )

    def _close_message(self) -> None:
        part = {"type": "output_text", "text": self.text_acc, "annotations": []}
        self.emit(
            "response.output_text.done",
            {"item_id": self.msg_id, "output_index": self.msg_oi, "content_index": 0, "text": self.text_acc},
        )
        self.emit(
            "response.content_part.done",
            {"item_id": self.msg_id, "output_index": self.msg_oi, "content_index": 0, "part": part},
        )
        self.emit(
            "response.output_item.done",
            {
                "output_index": self.msg_oi,
                "item": {
                    "id": self.msg_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [part],
                },
            },
        )
        self.part_open = False

    def _tools(self, delta: dict) -> None:
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = int(tc.get("index") or 0)
            if idx not in self.tools:
                if self.part_open:
                    self._close_message()
                self.tools[idx] = {
                    "id": rand_id("fc_"),
                    "oi": self.next_oi,
                    "next_oi": self.next_oi + 0,
                    "call_id": str(tc.get("id") or "") or rand_id("call_"),
                    "name": str((tc.get("function") or {}).get("name") or ""),
                    "args": "",
                }
                self.next_oi += 1
                self.emit(
                    "response.output_item.added",
                    {
                        "output_index": self.tools[idx]["oi"],
                        "item": self._tool_item(self.tools[idx], "in_progress"),
                    },
                )
            t = self.tools[idx]
            if tc.get("id"):
                t["call_id"] = str(tc["id"])
            fname = (tc.get("function") or {}).get("name")
            if fname:
                t["name"] += str(fname)
            frag = str((tc.get("function") or {}).get("arguments") or "")
            if frag:
                t["args"] += frag
                self.emit(
                    "response.function_call_arguments.delta",
                    {"item_id": t["id"], "output_index": t["oi"], "delta": frag},
                )

    def finalize(self) -> None:
        if self.done:
            return
        self.done = True
        if not self.started:
            self._start()
        if self.rs_open:
            self._close_reasoning()
        if self.part_open:
            self._close_message()
        elif not self.tools:
            self._open_empty()
            self._close_message()
        for t in self.tools.values():
            self.emit(
                "response.function_call_arguments.done",
                {"item_id": t["id"], "output_index": t["oi"], "arguments": t["args"]},
            )
            self.emit(
                "response.output_item.done",
                {"output_index": t["oi"], "item": self._tool_item(t, "completed")},
            )
        status = self._final_status()
        self.emit("response.completed", {"response": self._skeleton(status, True)})

    def _open_empty(self) -> None:
        self.part_open = True
        self.msg_oi = self.next_oi
        self.next_oi += 1
        self.emit(
            "response.output_item.added",
            {
                "output_index": self.msg_oi,
                "item": {
                    "id": self.msg_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            },
        )
        self.emit(
            "response.content_part.added",
            {
                "item_id": self.msg_id,
                "output_index": self.msg_oi,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        )


class AnthropicStream(_Base):
    """chat chunk → Anthropic Messages 事件流。"""

    def __init__(self, emit: Callable[[str, dict], None], model: str = "", est_input: int = 0) -> None:
        super().__init__()
        self.emit = emit
        self.model = model
        self.est_input = est_input
        self.started = False
        self.done = False
        self.finish: str | None = None
        self.next_index = 0
        self.think_idx: int | None = None
        self.text_idx: int | None = None
        self.text_acc = ""
        self.think_acc = ""
        self.tools: dict[int, dict] = {}
        self.msg_id = rand_id("msg_")

    def _handle_chunk(self, c: dict) -> None:
        if not self.started:
            if not self.model:
                self.model = str(c.get("model") or "")
            self._start()
        if isinstance(c.get("usage"), dict):
            self.usage = c["usage"]
        if c.get("error"):
            self.done = True
            self.emit(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": str((c.get("error") or {}).get("message") or "upstream error"),
                    },
                },
            )
            return
        choice = (c.get("choices") or [None])[0]
        if not isinstance(choice, dict):
            return
        if choice.get("finish_reason"):
            self.finish = str(choice["finish_reason"])
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            return
        self._reasoning(delta)
        self._content(delta)
        self._tools(delta)

    def _start(self) -> None:
        self.started = True
        self.emit(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": self.est_input, "output_tokens": 0},
                },
            },
        )

    def _reasoning(self, delta: dict) -> None:
        rs = delta.get("reasoning_content") or delta.get("reasoning")
        if not isinstance(rs, str) or not rs:
            return
        if self.text_idx is not None:
            self.emit("content_block_stop", {"type": "content_block_stop", "index": self.text_idx})
            self.text_idx = None
        if self.think_idx is None:
            self.think_idx = self.next_index
            self.next_index += 1
            self.emit(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.think_idx,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
        self.think_acc += rs
        self.emit(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.think_idx,
                "delta": {"type": "thinking_delta", "thinking": rs},
            },
        )

    def _content(self, delta: dict) -> None:
        content = delta.get("content")
        if not isinstance(content, str) or not content:
            return
        if self.think_idx is not None:
            self.emit("content_block_stop", {"type": "content_block_stop", "index": self.think_idx})
            self.think_idx = None
        if self.text_idx is None:
            self.text_idx = self.next_index
            self.next_index += 1
            self.emit(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.text_idx,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        self.text_acc += content
        self.emit(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.text_idx,
                "delta": {"type": "text_delta", "text": content},
            },
        )

    def _tools(self, delta: dict) -> None:
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = int(tc.get("index") or 0)
            if idx not in self.tools:
                if self.text_idx is not None:
                    self.emit("content_block_stop", {"type": "content_block_stop", "index": self.text_idx})
                    self.text_idx = None
                if self.think_idx is not None:
                    self.emit("content_block_stop", {"type": "content_block_stop", "index": self.think_idx})
                    self.think_idx = None
                blk = self.next_index
                self.next_index += 1
                self.tools[idx] = {
                    "idx": blk,
                    "id": str(tc.get("id") or "") or rand_id("toolu_"),
                    "name": str((tc.get("function") or {}).get("name") or ""),
                    "args": "",
                }
                self.emit(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": blk,
                        "content_block": {
                            "type": "tool_use",
                            "id": self.tools[idx]["id"],
                            "name": self.tools[idx]["name"],
                            "input": {},
                        },
                    },
                )
            t = self.tools[idx]
            if tc.get("id"):
                t["id"] = str(tc["id"])
            fname = (tc.get("function") or {}).get("name")
            if fname:
                t["name"] += str(fname)
            frag = str((tc.get("function") or {}).get("arguments") or "")
            if frag:
                t["args"] += frag
                self.emit(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": t["idx"],
                        "delta": {"type": "input_json_delta", "partial_json": frag},
                    },
                )

    def _stop_reason(self) -> str:
        return {"tool_calls": "tool_use", "length": "max_tokens"}.get(self.finish or "", "end_turn")

    def finalize(self) -> None:
        if self.done:
            return
        self.done = True
        if not self.started:
            self._start()
        if self.text_acc == "" and not self.tools and self.think_acc == "":
            self.text_idx = self.next_index
            self.next_index += 1
            self.emit(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.text_idx,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        for idx in [self.think_idx, self.text_idx] + [t["idx"] for t in self.tools.values()]:
            if idx is not None:
                self.emit("content_block_stop", {"type": "content_block_stop", "index": idx})
        self.emit(
            "message_delta",
            {
                "delta": {"stop_reason": self._stop_reason(), "stop_sequence": None},
                "usage": {"output_tokens": int((self.usage or {}).get("completion_tokens") or 0)},
            },
        )
        self.emit("message_stop", {})


# ============================================================ 合成整段流
