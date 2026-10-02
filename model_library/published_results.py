"""Read-only presentation of evidence published beside a catalog spec.

The canonical evidence verifier owns all judgements. This adapter only locates
public runs and describes their recorded scope; it never reads Workbench state.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
import shlex

from release_spec import load_spec
from release_spec.baseline_policy import SUPPORTED_POLICY_DIGESTS
from release_spec.evidence_v2 import evidence_summary, verify_evidence
from release_spec.measurement import load_measurement_bytes, parse_strict_json, read_stable_bytes
from release_spec.package import allowed_path
from release_spec.run_record import _time
from release_spec import serving
from .state import checked_id
from scripts.terminal_format import TerminalWriter


def _read_run(repo: Path, spec: dict, directory: Path) -> dict:
    relative = directory.relative_to(repo).as_posix()
    suite, run_id = directory.parts[-3], directory.name
    result = {"path": relative, "suite": suite, "run_id": run_id, "status": "unverified"}
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("expected a regular published run directory")
        if not allowed_path(spec["spec_id"], relative + "/run.json"):
            raise ValueError("unsupported published run path")
        spec_path = repo / "releases" / f"{spec['spec_id']}.json"
        run_path = directory / "run.json"
        raw_run = read_stable_bytes(run_path, label="published run")
        run = parse_strict_json(raw_run, label="published run")
        verified = verify_evidence(spec_path, run_path, repo / "results")
        if verified["spec_id"] != spec["spec_id"] or verified["run_id"] != run_id:
            raise ValueError("published directory and verified run identity differ")
        evaluation = verified["evaluation"]
        if evaluation.get("suite", "baseline-v1") != suite:
            raise ValueError("published directory and recorded policy suite differ")
        # Optional packaged summaries are checked, never trusted over evidence.
        for filename in ("evaluation.json", "summary.json"):
            path = directory / filename
            if path.exists() or path.is_symlink():
                document = serving.load_json(path)
                if not isinstance(document, dict):
                    raise ValueError(f"{filename} must be a structured evidence document")
                expected = (evaluation if filename == "evaluation.json" else
                            evidence_summary(verified, spec, document.get("archive_observation")))
                if document != expected:
                    raise ValueError(f"{filename} differs from verified evidence")
        measurements = {}
        for operation, digest in evaluation["measurement_sha256"].items():
            raw = read_stable_bytes(directory / f"{operation}.json", label="published measurement")
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("published measurement changed during inspection")
            measurements[operation] = load_measurement_bytes(raw)
        if read_stable_bytes(run_path, label="published run") != raw_run:
            raise ValueError("published run changed during inspection")
        gates = run["gates"]
        result.update(status="verified", outcome=verified["outcome"], evaluation=evaluation,
                      started_at=gates[0]["started_at"] if gates else None,
                      ended_at=gates[-1]["ended_at"] if gates else None,
                      measurements=measurements, error_codes=run["error_codes"])
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        result["reason"] = " ".join(str(exc).split())
    return result


def collect(repo: str | Path, spec_id: str) -> dict:
    """Inspect only results/<suite>/<selected spec>/<run>; isolate bad runs."""
    repo = Path(repo).absolute()
    spec_id = checked_id(spec_id)
    report = {"runs": [], "issues": []}
    try:
        spec = load_spec(repo / "releases" / f"{spec_id}.json")
        if spec["spec_id"] != spec_id:
            raise ValueError("catalog filename and spec identity differ")
        if spec["schema_version"] == 1:
            report["historical"] = bool(spec.get("evidence"))
            return report
        results = repo / "results"
        if results.is_symlink():
            raise ValueError("published results directory must not be a symlink")
        if not results.exists():
            return report
        for suite in sorted(results.iterdir()):
            if suite.is_symlink():
                report["issues"].append(f"Skipped linked results directory: {suite.name}")
                continue
            if not suite.is_dir():
                continue
            selected = suite / spec_id
            if selected.is_symlink():
                report["issues"].append(f"Skipped linked spec results: {suite.name}")
                continue
            if not selected.exists():
                continue
            if suite.name not in SUPPORTED_POLICY_DIGESTS:
                report["issues"].append(f"Unsupported published result suite: {suite.name}")
                continue
            for directory in sorted(selected.iterdir()):
                if directory.is_dir() or directory.is_symlink():
                    report["runs"].append(_read_run(repo, spec, directory))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        report["issues"].append(" ".join(str(exc).split()))
    # Never choose a favourable outcome: order verified runs by recorded time.
    report["runs"].sort(key=lambda run: (_time(run["ended_at"], "run end") if run.get("ended_at") else
                                       datetime.min.replace(tzinfo=timezone.utc), run["path"]), reverse=True)
    return report


def workload_lines(run: dict) -> list[str]:
    measurements = run["measurements"]
    lines = []
    benchmark = measurements.get("benchmark-serving")
    if benchmark:
        data = benchmark["benchmark-serving"]
        levels = ", ".join(str(level["concurrency"]) for level in data["levels"]) or "not recorded"
        lines.append(f"Benchmark: {data['prompt_style']} prompts, {data['input_tokens']} input / "
                     f"{data['output_tokens']} output tokens; concurrency {levels}; {benchmark['completion']}")
    accuracy = measurements.get("evaluate-gsm8k")
    if accuracy:
        data = accuracy["evaluate-gsm8k"]
        lines.append(f"Accuracy: {data['dataset_id']} {data['subset']}/{data['split']}, "
                     f"{data['measured_sample_count']}/{data['requested_sample_count']} samples; {accuracy['completion']}")
    if not lines:
        lines.append("Workload: no benchmark or accuracy measurement supplied")
    return lines


def compact_lines(report: dict | None, *, now=None) -> list[str]:
    from .catalog import age_seconds, age_text
    if report is None:
        return ["Published results: not inspected"]
    if report.get("historical"):
        return ["Published results: historical schema-1; inspect evidence references in Published results"]
    runs, issues = report["runs"], report["issues"]
    if not runs and not issues:
        return ["Published results: none supplied"]
    lines = [f"Published results (historical): {len(runs)} run(s)"]
    verified = [run for run in runs if run["status"] == "verified"]
    dated = [run for run in verified if run.get("ended_at")]
    if dated:
        run = dated[0]
        date = run["ended_at"]
        lines.append(f"Latest dated run (verified): {run['suite']} {run['outcome']} · {date[:10]} "
                     f"({age_text(age_seconds(date, now))})")
        lines.append(workload_lines(run)[0])
    undated = len(verified) - len(dated)
    if undated:
        lines.append(f"{undated} verified run(s) have no recorded run time")
    unavailable = len(runs) - len(verified)
    if unavailable or issues:
        lines.append(f"{unavailable} unverified run(s), {len(issues)} inspection issue(s); see Published results")
    return lines


def render(report: dict, spec_id: str, *, writer: TerminalWriter | None = None) -> None:
    out = writer or TerminalWriter()
    def metric(value, unit):
        return f"{value} {unit}" if value is not None else "not recorded"

    out.emit(f"Published results for spec {spec_id[:12]}")
    out.emit("Historical measurements for this exact spec and recorded workloads. "
             "They do not establish current service health or suitability for another workload.")
    if report.get("historical"):
        out.emit("Schema-1 evidence retains its historical format. Inspect its bound evidence references:")
        print(f"./pulsar spec show --historical \\\n  --file releases/{spec_id}.json", file=out.stream)
        return
    if not report["runs"] and not report["issues"]:
        out.emit("None supplied. Published results are optional and do not gate catalog membership or start.")
    for issue in report["issues"]:
        out.field("Unavailable", issue)
    if report["issues"]:
        out.emit("Check this catalog checkout or ask its maintainer to repair the published evidence, then retry.")
    for run in report["runs"]:
        out.blank()
        out.emit(f"{run['suite']} / {run['run_id']}")
        out.field("Source", run["path"])
        if run["status"] != "verified":
            out.field("Unverified", run["reason"])
            out.emit("No outcome is asserted. Check this catalog checkout or ask its maintainer to repair the published evidence, then retry.")
            continue
        out.field("Evidence", "verified against the selected spec and recorded policy")
        out.field("Run outcome", run["outcome"])
        out.field("Policy outcome", run["evaluation"]["outcome"])
        out.field("Run started", run["started_at"] or "unknown (no recorded producer time)")
        out.field("Run ended", run["ended_at"] or "unknown (no recorded producer time)")
        if run["error_codes"]:
            out.field("Run errors", ", ".join(run["error_codes"]))
        for line in workload_lines(run):
            out.emit(line)
        for criterion, outcome in run["evaluation"]["outcomes"].items():
            out.field(criterion, outcome, indent=2)
        if run["suite"] == "baseline-v2":
            out.emit("Capture repeatability is diagnostic; differences do not fail baseline-v2.")
        benchmark = run["measurements"].get("benchmark-serving")
        if benchmark:
            for level in benchmark["benchmark-serving"]["levels"]:
                out.emit(f"Concurrency {level['concurrency']}: {level['measured_request_count']}/"
                         f"{level['requested_request_count']} requests; {level['completion']}; "
                         f"aggregate {metric(level['aggregate_tps'], 'tok/s')}; "
                         f"median decode {metric(level['decode_tps_p50'], 'tok/s')}; "
                         f"p95 TTFT {metric(level['ttft_p95_ms'], 'ms')}", initial_indent="  ")
        accuracy = run["measurements"].get("evaluate-gsm8k")
        if accuracy:
            data = accuracy["evaluate-gsm8k"]
            out.emit(f"Accuracy result: {data['correct_count']}/{data['measured_sample_count']} correct, "
                     f"{data['request_error_count']} request errors; selection {data['selection']}; "
                     f"temperature {data['temperature']}; max completion {data['max_completion_tokens']} tokens; "
                     f"reasoning {data['reasoning_mode']}")
            out.field("Dataset revision", data["dataset_revision"])
        soak = run["measurements"].get("validate-soak")
        if soak:
            data = soak["validate-soak"]
            out.emit(f"Soak: {data['duration_seconds']} s; concurrency {data['concurrency']}; "
                     f"{data['completed_requests']} completed, {data['request_error_count']} request errors; "
                     f"{soak['completion']}")
        out.emit("Full evidence summary (JSON):")
        # Wrap only between arguments, leaving every identity pasteable.
        arguments = ["./pulsar evidence summary", f"--spec-file releases/{spec_id}.json",
                     "--run " + shlex.quote(run["path"] + "/run.json"), "--evidence-root results --json"]
        print(" \\\n  ".join(arguments), file=out.stream)
