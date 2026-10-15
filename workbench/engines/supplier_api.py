"""EvalScope public API-plugin extension for strict supplier text acceptance.

No scheduler is copied or patched. Only HTTP response interpretation differs
from EvalScope's general-purpose OpenAI plugin.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp
from evalscope.perf.plugin.api.openai_api import OpenaiPlugin
from evalscope.perf.plugin.registry import register_api, register_dataset
from evalscope.perf.plugin.datasets.workload_trace import WorkloadTraceDatasetPlugin
from evalscope.perf.utils.benchmark_util import BenchmarkData

from workbench.engines.supplier_stream import SSEDecoder, StreamContractError, StrictTextStream


@register_dataset("workbench_supplier_trace")
class SupplierTraceDataset(WorkloadTraceDatasetPlugin):
    """Use the official replay implementation and retain a conservative origin.

    EvalScope does not expose its private scheduling anchor. The timestamp below
    is taken when the final request is yielded, before the official dispatcher
    anchors its clock. Consequently measured dispatch delay cannot hide startup
    overhead by moving the window to the first HTTP request.
    """
    def build_messages(self):
        records = list(super().build_messages())
        for index, record in enumerate(records):
            if index == len(records) - 1:
                origin = {"schema_version": 1, "planned_requests": len(records), "engine_version": "evalscope 1.13.0", "dispatch_method": "official workload_trace fixed offsets / open_loop", "origin_kind": "requests_ready_before_native_dispatch",
                          "dispatch_ready_epoch": time.time(), "dispatch_ready_monotonic": time.perf_counter(),
                          "note": "Conservative lower bound for the private native scheduler anchor; includes handoff overhead."}
                path = os.environ.get("WORKBENCH_SUPPLIER_SCHEDULE")
                if path:
                    Path(path).write_text(json.dumps(origin, indent=2) + "\n", encoding="utf-8")
            yield record


@register_api("workbench_strict_supplier")
class StrictSupplierPlugin(OpenaiPlugin):
    def __init__(self, param):
        super().__init__(param)
        self.sequence = 0
        self.first_start = None

    def parse_responses(self, responses, request=None, **kwargs):
        # Usage is optional; absence is unknown, never a tokenizer download.
        for item in reversed(responses or []):
            usage = item.get("usage") if isinstance(item, dict) else None
            if isinstance(usage, dict):
                values = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
                return tuple(v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0 for v in values)
        return 0, 0

    async def process_request(self, client_session, url, headers, body):
        self.sequence += 1
        sequence = self.sequence
        body = dict(body)
        request_id = body.pop("_workbench_request_id", f"request-{sequence:06d}")
        request_data = json.dumps(body, ensure_ascii=False)
        stream = StrictTextStream()
        decoder = SSEDecoder()
        auth = next((str(value) for key, value in headers.items() if key.lower() == "authorization"), "")
        secret = auth[7:] if auth.lower().startswith("bearer ") else ""
        retained_request = request_data.replace(secret, "***") if secret else request_data
        output = BenchmarkData(request=retained_request, is_stream=True)
        # Explicit clock origin: immediately before session.post(), includes
        # connection/pool wait, DNS, TLS and response delay, excludes scheduling.
        started_epoch = time.time()
        started = time.perf_counter()
        if self.first_start is None:
            self.first_start = started
        output.start_time = started
        code = None
        failure = None
        try:
            async with client_session.post(
                url=url, data=request_data, headers={"Content-Type": "application/json", **headers},
                allow_redirects=False,
            ) as response:
                code = response.status
                output.status_code = code
                if code != 200:
                    raise StreamContractError(f"HTTP_{code}")
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    raise StreamContractError("NON_STREAM_RESPONSE")
                async for chunk in response.content.iter_any():
                    for payload in decoder.feed(chunk):
                        stream.accept(payload, time.perf_counter() - started)
                    if stream.done:
                        break
                if not stream.done:
                    for payload in decoder.feed(b"", final=True):
                        stream.accept(payload, time.perf_counter() - started)
                stream.validate()
                output.success = True
        except asyncio.TimeoutError:
            failure = "TIMEOUT"
        except StreamContractError as error:
            failure = str(error)
        except UnicodeError:
            failure = "INVALID_UTF8"
        except aiohttp.ClientError:
            failure = "NETWORK_ERROR"
        except Exception:
            failure = "CLIENT_ERROR"
        finally:
            completed = time.perf_counter()
            output.completed_time = completed
            output.query_latency = completed - started
            output.first_chunk_latency = stream.first_generated_seconds or 0.0
            output.inter_chunk_latency = [b - a for a, b in zip(stream.output_times, stream.output_times[1:])]
            output.n_decode_chunks = len(stream.output_times)
            # Native EvalScope stores response_messages as pickle/base64 in
            # SQLite. Retain only validated usage numbers: text replacement or
            # a later byte scan cannot catch credentials split across chunks,
            # used as dictionary keys, or encoded by an upstream response.
            usage = {name: value for name in ("prompt_tokens", "completion_tokens")
                     if isinstance((value := (stream.usage or {}).get(name)), int)
                     and not isinstance(value, bool) and value >= 0}
            output.generated_text = ""
            output.response_messages = [{"usage": usage}] if stream.usage is not None else []
            output.error = failure
            output.success = output.success and failure is None
            output.prompt_tokens, output.completion_tokens = self.parse_responses(output.response_messages)
            rpm = float(os.environ.get("WORKBENCH_SUPPLIER_RPM", "0"))
            scheduled_offset = (sequence - 1) * 60 / rpm if rpm else None
            origin = self.first_start
            schedule_path = os.environ.get("WORKBENCH_SUPPLIER_SCHEDULE")
            if rpm and schedule_path and Path(schedule_path).is_file():
                origin = json.loads(Path(schedule_path).read_text(encoding="utf-8"))["dispatch_ready_monotonic"]
            record = {
                "schema_version": 1,
                "sequence": sequence,
                "request_id": request_id,
                "completed_epoch": started_epoch + completed - started,
                "started_epoch": started_epoch,
                "started_monotonic": started,
                "completed_monotonic": completed,
                "start_offset_seconds": started - origin,
                "scheduled_offset_seconds": scheduled_offset,
                "schedule_delay_ms": (started - origin - scheduled_offset) * 1000 if rpm else None,
                "ttft_seconds": stream.first_generated_seconds,
                "ttfot_seconds": stream.first_content_seconds,
                "latency_seconds": output.query_latency,
                "http_status": code,
                "strict_success": output.success,
                "error_code": failure,
                "done_received": stream.done,
                "finish_reason": stream.finish_reason if stream.finish_reason in (None, "stop", "length", "content_filter", "tool_calls") else "other",
                "body_nonempty": bool(stream.text.strip()),
                "usage_present": stream.usage is not None,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            }
            evidence = os.environ.get("WORKBENCH_SUPPLIER_RECORDS")
            if evidence:
                # One event loop, append without an await: no concurrent writes.
                with Path(evidence).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return output
