"""Regression tests for supplier acceptance, evidence completeness, and RPM."""

import json
from pathlib import Path
import tempfile
import unittest

from workbench.reports.supplier import build_report, summarize_phase


def record(sequence, offset=0.0, *, success=True, latency=0.2, ttft=.04, ttfot=.12):
    return {"schema_version": 1, "sequence": sequence, "request_id": f"request-{sequence:06d}",
            "started_epoch": 1800000000 + offset, "completed_epoch": 1800000000 + offset + latency,
            "started_monotonic": 1000 + offset, "completed_monotonic": 1000 + offset + latency,
            "start_offset_seconds": offset, "scheduled_offset_seconds": (sequence - 1) * .3,
            "schedule_delay_ms": (offset - (sequence - 1) * .3) * 1000,
            "ttft_seconds": ttft if success else None, "ttfot_seconds": ttfot if success else None,
            "latency_seconds": latency, "http_status": 200 if success else 429,
            "strict_success": success, "error_code": None if success else "HTTP_429",
            "done_received": success, "finish_reason": "stop" if success else None,
            "body_nonempty": success, "usage_present": success,
            "prompt_tokens": 1 if success else None, "completion_tokens": 5 if success else None}


def write_phase(directory, records, *, schedule=False, native=True):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "supplier-requests.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    if native:
        leaf = directory / "native" / "engine-output"
        leaf.mkdir(parents=True, exist_ok=True)
        (leaf / "benchmark_summary.json").write_text(json.dumps({
            "Total Requests": len(records), "Success Requests": sum(row["strict_success"] for row in records),
            "Failed Requests": sum(not row["strict_success"] for row in records), "Avg TTFT (ms)": 40}))
        (leaf / "perf_report.html").write_text("<!doctype html><title>Native test fixture</title>")
    if schedule:
        (directory / "supplier-schedule.json").write_text(json.dumps({
            "schema_version": 1, "origin_kind": "requests_ready_before_native_dispatch",
            "dispatch_ready_epoch": 1800000000, "dispatch_ready_monotonic": 1000,
            "planned_requests": len(records)}))


def phase(name="latency", count=2, rpm=None, window=None):
    return {"name": name, "count": count, "rpm": rpm, "window_seconds": window}


def manifest(directory, phases, *, thresholds=None, runs=None):
    data = {"schema_version": 1, "pack_version": "2.0.0", "engine": {"name": "evalscope", "version": "1.13.0"},
            "demo": True, "target": {"supplier": "local-simulation", "model": "local-mock",
                "region": "loopback", "reasoning_mode": "synthetic", "thresholds": thresholds or {}},
            "phases": phases, "planned_requests": sum(p["count"] for p in phases),
            "phase_runs": runs if runs is not None else [
                {"name": p["name"], "status": "completed", "exit_code": 0, "run_dir": p["name"]} for p in phases]}
    (directory / "manifest.json").write_text(json.dumps(data))
    return data


class SupplierReportTests(unittest.TestCase):
    def test_full_ten_minute_fixed_schedule_and_drain_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [record(i + 1, i * .3 + .001, latency=2) for i in range(2000)]
            write_phase(root, rows, schedule=True)
            result = summarize_phase(root, phase("rpm200", 2000, 200, 600))
            self.assertEqual(result["evidence_status"], "complete")
            self.assertEqual(result["valid_success"], 2000)
            rate = result["rate"]
            self.assertTrue(rate["met"])
            self.assertEqual(rate["actual_start_rpm"], 200)
            self.assertEqual([m["actual_starts"] for m in rate["minutes"]], [200] * 10)
            self.assertGreater(rate["completed_after_window"], 0)
            self.assertLess(rate["successful_completed_rpm_in_window"], rate["actual_start_rpm"])

    def test_delayed_start_does_not_reset_the_measurement_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root, [record(i + 1, i * .3 + 61) for i in range(400)], schedule=True)
            result = summarize_phase(root, phase("rpm200", 400, 200, 120))
            self.assertFalse(result["rate"]["met"])
            self.assertEqual(result["rate"]["minutes"][0]["actual_starts"], 0)
            self.assertGreater(result["rate"]["schedule_delay_ms"]["p95"], 60000)
            self.assertGreater(result["rate"]["late_starts"], 0)

    def test_burst_is_not_sustained_200_rpm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root, [record(i + 1, .01 + i * .00001) for i in range(200)], schedule=True)
            result = summarize_phase(root, phase("rpm200", 200, 200, 60))
            self.assertEqual(result["rate"]["actual_start_rpm"], 200)
            self.assertFalse(result["rate"]["met"])

    def test_missing_schedule_does_not_guess_origin_from_first_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root, [record(1), record(2, .3)])
            result = summarize_phase(root, phase("rpm200", 2, 200, .6))
            self.assertIsNone(result["rate"]["met"])
            self.assertIsNone(result["rate"]["actual_start_rpm"])
            self.assertIn("SCHEDULE_ORIGIN_MISSING", result["issues"])

    def test_missing_and_duplicate_request_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root, [record(1)])
            self.assertEqual(summarize_phase(root, phase())["evidence_status"], "incomplete")
            write_phase(root, [record(1), record(1, .3)])
            self.assertIn("SEQUENCE_INCOMPLETE_OR_DUPLICATE", summarize_phase(root, phase())["issues"])

    def test_native_totals_must_agree_with_request_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root, [record(1), record(2, .3)])
            native = root / "native/engine-output/benchmark_summary.json"
            native.write_text(json.dumps({"Total Requests": 200, "Success Requests": 200, "Failed Requests": 0}))
            self.assertIn("NATIVE_COUNTS_MISMATCH", summarize_phase(root, phase())["issues"])

    def test_missing_done_never_counts_as_success_even_if_engine_flag_lies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = record(1)
            row["done_received"] = False
            write_phase(root, [row])
            result = summarize_phase(root, phase(count=1))
            self.assertEqual(result["valid_success"], 0)
            self.assertEqual(result["evidence_status"], "incomplete")
            self.assertEqual(result["errors"], {"CONTRACT_MISMATCH": 1})

    def test_failure_with_complete_timings_is_complete_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = record(1, success=False)
            row.update(http_status=200, error_code="MISSING_DONE", body_nonempty=True, finish_reason="stop")
            write_phase(root, [row])
            result = summarize_phase(root, phase(count=1))
            self.assertEqual(result["evidence_status"], "complete")
            self.assertEqual(result["errors"], {"MISSING_DONE": 1})
            self.assertIsNone(result["ttft_seconds"]["p95"])

    def test_partial_failures_without_success_threshold_remain_measurement_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1), record(2, .3, success=False)])
            manifest(root, [phase()])
            result = build_report(root)
            self.assertTrue(result["complete"])
            self.assertEqual(result["assessment"], "measurement_only")
            self.assertEqual(result["phases"][0]["failed"], 1)
            self.assertEqual(result["phases"][0]["ttft_seconds"]["count"], 1)

    def test_no_success_is_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1, success=False)])
            manifest(root, [phase(count=1)])
            result = build_report(root)
            self.assertTrue(result["complete"])
            self.assertEqual(result["assessment"], "not_passed")

    def test_configured_success_threshold_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1), record(2, .3, success=False)])
            manifest(root, [phase()], thresholds={"min_success_rate": .99})
            result = build_report(root)
            self.assertEqual(result["assessment"], "not_passed")
            self.assertIn("latency:MIN_SUCCESS_RATE_NOT_MET", result["failures"])

    def test_configured_ttft_threshold_is_not_confused_with_first_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1)])
            manifest(root, [phase(count=1)], thresholds={"ttft_p95_seconds": .05, "ttfot_p95_seconds": .1})
            result = build_report(root)
            self.assertEqual(result["assessment"], "not_passed")
            self.assertEqual(result["failures"], ["latency:TTFOT_P95_SECONDS_NOT_MET"])

    def test_ttft_p50_uses_strict_less_than_without_rounding_the_decision(self):
        for actual, expected in ((.999, True), (1.0, False), (1.001, False)):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_phase(root / "latency", [record(1, latency=2, ttft=actual, ttfot=1.5)])
                manifest(root, [phase(count=1)], thresholds={"ttft_p50_seconds": 1.0})
                result = build_report(root)
                check = result["phases"][0]["ttft_p50_check"]
                self.assertEqual(check, {"actual_seconds": actual, "limit_seconds": 1.0,
                                         "operator": "<", "met": expected})
                self.assertEqual(result["assessment"], "measurement_only" if expected else "not_passed")
                self.assertEqual(result["thresholds"]["ttft_p50_seconds"], 1.0)

    def test_p50_judges_latency_and_200rpm_separately_not_pooled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(i + 1, i * 3, latency=2, ttft=.2, ttfot=1.5) for i in range(5)])
            write_phase(root / "rpm200", [record(i + 1, i * .3 + .001, latency=2, ttft=1.2, ttfot=1.5) for i in range(2)], schedule=True)
            manifest(root, [phase(count=5), phase("rpm200", 2, 200, .6)], thresholds={"ttft_p50_seconds": 1.0})
            result = build_report(root)
            checks = {p["name"]: p["ttft_p50_check"] for p in result["phases"]}
            self.assertTrue(checks["latency"]["met"])
            self.assertFalse(checks["rpm200"]["met"])
            self.assertEqual(checks["latency"]["actual_seconds"], .2)
            self.assertEqual(checks["rpm200"]["actual_seconds"], 1.2)
            self.assertEqual(result["failures"], ["rpm200:TTFT_P50_SECONDS_NOT_MET"])
            self.assertEqual(result["assessment"], "not_passed")

    def test_connectivity_and_ramp_stages_do_not_receive_p50_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planned = [phase("connectivity", 1), phase(count=1), phase("rpm50", 2, 50, 2.4),
                       phase("rpm100", 2, 100, 1.2), phase("rpm200", 2, 200, .6)]
            for item in planned:
                is_measured = item["name"] in {"latency", "rpm200"}
                interval = 60 / item["rpm"] if item["rpm"] else 3
                rows = [record(i + 1, i * interval + .001, latency=6,
                               ttft=.5 if is_measured else 4, ttfot=5) for i in range(item["count"])]
                write_phase(root / item["name"], rows, schedule=bool(item["rpm"]))
            manifest(root, planned, thresholds={"ttft_p50_seconds": 1.0, "min_success_rate": 1.0})
            result = build_report(root)
            checks = {p["name"]: p["ttft_p50_check"] for p in result["phases"]}
            self.assertTrue(checks["latency"]["met"])
            self.assertTrue(checks["rpm200"]["met"])
            for name in ("connectivity", "rpm50", "rpm100"):
                self.assertIsNone(checks[name])
            self.assertEqual(result["assessment"], "passed")

    def test_p50_is_first_generated_token_not_first_answer_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1, latency=5, ttft=.25, ttfot=4)])
            manifest(root, [phase(count=1)], thresholds={"ttft_p50_seconds": 1.0})
            result = build_report(root)
            self.assertEqual(result["phases"][0]["ttft_p50_check"]["actual_seconds"], .25)
            self.assertTrue(result["phases"][0]["ttft_p50_check"]["met"])
            self.assertEqual(result["phases"][0]["ttfot_seconds"]["p50"], 4)

    def test_historical_manifest_without_p50_keeps_legacy_p95_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1, latency=2, ttft=1.0, ttfot=1.5)])
            manifest(root, [phase(count=1)], thresholds={"ttft_p95_seconds": 1.0})
            result = build_report(root)
            self.assertIsNone(result["thresholds"]["ttft_p50_seconds"])
            self.assertIsNone(result["phases"][0]["ttft_p50_check"])
            self.assertEqual(result["assessment"], "passed")

    def test_p50_pass_with_partial_failure_cannot_approve_without_success_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1), record(2, .3, success=False)])
            manifest(root, [phase()], thresholds={"ttft_p50_seconds": 1.0})
            result = build_report(root)
            self.assertTrue(result["phases"][0]["ttft_p50_check"]["met"])
            self.assertEqual(result["phases"][0]["success_rate"], .5)
            self.assertEqual(result["assessment"], "measurement_only")
            workbench = json.loads((root / "workbench-summary.json").read_text())
            self.assertEqual(workbench["conclusion"], "needs_more_data")
            data = json.loads((root / "report-data.json").read_text())
            self.assertIn("P50 分项达标不等于整体准入通过", data["sections"][0]["body"])

    def test_p50_without_success_gate_stays_measurement_even_with_p95(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1)])
            manifest(root, [phase(count=1)], thresholds={"ttft_p50_seconds": 1.0, "ttft_p95_seconds": 1.0})
            self.assertEqual(build_report(root)["assessment"], "measurement_only")

    def test_p50_does_not_hide_an_explicit_success_rate_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1), record(2, .3, success=False)])
            manifest(root, [phase()], thresholds={"ttft_p50_seconds": 1.0, "min_success_rate": .99})
            result = build_report(root)
            self.assertTrue(result["phases"][0]["ttft_p50_check"]["met"])
            self.assertEqual(result["assessment"], "not_passed")
            self.assertIn("latency:MIN_SUCCESS_RATE_NOT_MET", result["failures"])

    def test_p50_without_success_samples_is_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1, success=False)])
            manifest(root, [phase(count=1)], thresholds={"ttft_p50_seconds": 1.0})
            result = build_report(root)
            self.assertIsNone(result["phases"][0]["ttft_p50_check"]["actual_seconds"])
            self.assertFalse(result["phases"][0]["ttft_p50_check"]["met"])
            self.assertEqual(result["assessment"], "not_passed")

    def test_incomplete_phase_keeps_p50_value_but_never_says_it_passed(self):
        for failure in ("native_missing", "request_missing", "run_failed"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = [record(1)] if failure == "request_missing" else [record(1), record(2, .3)]
                write_phase(root / "latency", rows, native=failure != "native_missing")
                runs = [{"name": "latency", "status": "failed", "exit_code": 124, "run_dir": "latency"}] if failure == "run_failed" else None
                manifest(root, [phase()], thresholds={"ttft_p50_seconds": 1.0}, runs=runs)
                result = build_report(root)
                check = result["phases"][0]["ttft_p50_check"]
                self.assertEqual(check["actual_seconds"], .04)
                self.assertIsNone(check["met"])
                self.assertEqual(result["assessment"], "incomplete")
                self.assertNotIn("latency:TTFT_P50_SECONDS_NOT_MET", result["failures"])
                data = json.loads((root / "report-data.json").read_text())
                card = next(m for m in data["metrics"] if m["label"] == "低负载 TTFT P50")
                self.assertIn("证据不足", card["note"])
                self.assertNotIn("达标", card["note"])
                self.assertEqual(card["tone"], "warn")
                self.assertIn("证据不足", data["rows"][0]["ttft_p50_check"])

    def test_html_promotes_both_p50s_and_preserves_p95_and_first_body(self):
        for actual in (.9996, .9999999):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_phase(root / "latency", [record(1, latency=2, ttft=actual, ttfot=1.5)])
                write_phase(root / "rpm200", [record(1, .001, latency=2, ttft=.9, ttfot=1.5)], schedule=True)
                manifest(root, [phase(count=1), phase("rpm200", 1, 200, .3)], thresholds={"ttft_p50_seconds": 1.0})
                result = build_report(root)
                data = json.loads((root / "report-data.json").read_text())
                metrics = {row["label"]: row for row in data["metrics"]}
                self.assertIn("低负载 TTFT P50", metrics)
                self.assertIn("200 RPM TTFT P50", metrics)
                shown = float(metrics["低负载 TTFT P50"]["value"].removesuffix(" s"))
                self.assertLess(shown, 1.0)
                self.assertIn("达标", metrics["低负载 TTFT P50"]["note"])
                keys = {column["key"] for column in data["columns"]}
                self.assertTrue({"ttft_p50", "ttft_p50_check", "ttft", "ttfot"}.issubset(keys))
                self.assertEqual(data["rows"][0]["ttfot"], "1.500 s")
                self.assertTrue(result["phases"][0]["ttft_p50_check"]["met"])

    def test_missing_phase_is_incomplete_not_supplier_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest(root, [phase("connectivity", 1), phase("latency", 2)], runs=[])
            result = build_report(root)
            self.assertFalse(result["complete"])
            self.assertEqual(result["assessment"], "incomplete")
            self.assertEqual(result["status"], "failed")

    def test_missing_native_evidence_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root, [record(1)], native=False)
            self.assertIn("NATIVE_SUMMARY_MISSING_OR_AMBIGUOUS", summarize_phase(root, phase(count=1))["issues"])

    def test_outputs_reuse_ui_contract_and_keep_relative_native_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_phase(root / "latency", [record(1)])
            manifest(root, [phase(count=1)])
            summary = build_report(root)
            data = json.loads((root / "report-data.json").read_text())
            workbench = json.loads((root / "workbench-summary.json").read_text())
            self.assertTrue(summary["demo"])
            self.assertIn("本地模拟", data["title"])
            self.assertEqual(data["rows"][0]["report"], "latency/native/engine-output/perf_report.html")
            self.assertEqual(workbench["schema_version"], 1)
            self.assertEqual(workbench["conclusion"], "needs_more_data")
            self.assertIn("window.__WORKBENCH_REPORT__", (root / "report.html").read_text())
            self.assertTrue((root / "requests.csv").is_file())

    def test_report_and_csv_do_not_export_body_or_arbitrary_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "sk-do-not-export-this-fixture-secret"
            row = record(1, success=False)
            row.update(prompt=secret, generated_text=secret, authorization=secret, error_code=secret)
            write_phase(root / "latency", [row])
            manifest(root, [phase(count=1)])
            build_report(root)
            for name in ("report.html", "report-data.json", "supplier-summary.json", "workbench-summary.json", "requests.csv"):
                self.assertNotIn(secret, (root / name).read_text())

    def test_malformed_records_and_nonfinite_timings_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = record(1)
            row["latency_seconds"] = float("nan")
            write_phase(root, [row])
            with (root / "supplier-requests.jsonl").open("a") as handle:
                handle.write("not json\n")
            result = summarize_phase(root, phase(count=1))
            self.assertEqual(result["evidence_status"], "incomplete")
            self.assertEqual(result["valid_success"], 0)

    def test_phase_path_cannot_escape_result_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest(root, [phase()], runs=[{"name": "latency", "status": "completed", "exit_code": 0, "run_dir": "../elsewhere"}])
            with self.assertRaisesRegex(ValueError, "within"):
                build_report(root)


if __name__ == "__main__":
    unittest.main()
