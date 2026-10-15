"""Strict text-stream contract; independent of the benchmark scheduler."""
from __future__ import annotations

import codecs
import json


class StreamContractError(ValueError):
    pass


class SSEDecoder:
    """Incremental UTF-8/SSE decoding, including fragmented CRLF and Unicode."""
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.lines = []

    def feed(self, chunk: bytes, final: bool = False) -> list[str]:
        self.buffer += self.decoder.decode(chunk, final=final)
        output = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            line = line.removesuffix("\r")
            if not line:
                if self.lines:
                    output.append("\n".join(self.lines))
                    self.lines = []
            elif line.startswith("data:"):
                self.lines.append(line[5:].removeprefix(" "))
        if final:
            if self.buffer.startswith("data:"):
                self.lines.append(self.buffer[5:].removeprefix(" ").removesuffix("\r"))
            if self.lines:
                output.append("\n".join(self.lines))
                self.lines = []
            self.buffer = ""
        return output


class StrictTextStream:
    def __init__(self):
        self.done = False
        self.finish_reason = None
        self.body_parts = []
        self.first_generated_seconds = None
        self.first_content_seconds = None
        self.output_times = []
        self.messages = []
        self.usage = None

    def accept(self, payload: str, elapsed: float) -> None:
        if self.done:
            raise StreamContractError("DATA_AFTER_DONE")
        if payload.strip() == "[DONE]":
            self.done = True
            return
        try:
            item = json.loads(payload)
        except (ValueError, TypeError) as error:
            raise StreamContractError("MALFORMED_STREAM") from error
        if not isinstance(item, dict) or item.get("error"):
            raise StreamContractError("API_STREAM_ERROR")
        choices = item.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise StreamContractError("UNSUPPORTED_CHOICES")
        if choices:
            choice = choices[0]
            if not isinstance(choice, dict):
                raise StreamContractError("MALFORMED_STREAM")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise StreamContractError("MALFORMED_STREAM")
            if delta.get("tool_calls") or delta.get("function_call"):
                raise StreamContractError("UNEXPECTED_TOOL_CALL")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise StreamContractError("NON_TEXT_CONTENT")
            content = content or ""
            reasoning = "".join(delta[k] for k in ("reasoning_content", "reasoning") if isinstance(delta.get(k), str))
            details = delta.get("reasoning_details")
            if isinstance(details, list):
                reasoning += "".join(d[k] for d in details if isinstance(d, dict) for k in ("text", "summary") if isinstance(d.get(k), str))
            if content.strip() or reasoning.strip():
                if self.first_generated_seconds is None:
                    self.first_generated_seconds = elapsed
                self.output_times.append(elapsed)
            if content.strip() and self.first_content_seconds is None:
                self.first_content_seconds = elapsed
            self.body_parts.append(content)
            finish = choice.get("finish_reason")
            if finish is not None:
                if self.finish_reason is not None and finish != self.finish_reason:
                    raise StreamContractError("CONFLICTING_FINISH")
                self.finish_reason = finish
        if isinstance(item.get("usage"), dict):
            self.usage = item["usage"]
        self.messages.append(item)

    @property
    def text(self):
        return "".join(self.body_parts)

    def validate(self):
        if not self.done:
            raise StreamContractError("MISSING_DONE")
        if self.finish_reason == "length":
            raise StreamContractError("TRUNCATED")
        if self.finish_reason != "stop":
            raise StreamContractError("MISSING_OR_INVALID_FINISH")
        if not self.text.strip():
            raise StreamContractError("EMPTY_BODY")
