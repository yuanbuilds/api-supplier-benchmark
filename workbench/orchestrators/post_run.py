"""Single post-run lifecycle for artifacts produced by benchmark engines."""

from __future__ import annotations

from pathlib import Path


def finalize_evalscope(run_dir: Path, include_html: bool) -> None:
    """Create summary, admission, and optional UI after a successful EvalScope run."""
    from workbench.reports.admission import write_admission
    from workbench.reports.evalscope import refresh_reports

    refresh_reports(run_dir, include_html=False)
    write_admission(run_dir)
    refresh_reports(run_dir, include_html=include_html)
