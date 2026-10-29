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
from release_spec.baseline_policy import SUPPORTED_POLICY_DIGESTS, load_supported_policy
from release_spec.evidence_v2 import evidence_summary, verify_evidence
from release_spec.measurement import load_measurement_bytes, parse_strict_json, read_stable_bytes
from release_spec.package import allowed_path
from release_spec.run_record import _time
from release_spec import serving
from .state import checked_id
from scripts.terminal_format import TerminalWriter

STACK_ROOT = Path(__file__).resolve().parents[1]


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
    result["policy_gates"] = _policy_gates(suite, evaluation.get("policy_digest"))
    return result


def _policy_gates(suite: str, digest: str | None) -> dict:
    """This Stack's fixed policy gates, when the run recorded the same policy.

    They add what recorded thresholds leave out, such as the required
    concurrency levels. An explanation is optional, so a missing or different
    policy leaves only the run's own thresholds and never unverifies a run.
    """
    try:
        policy, found = load_supported_policy(STACK_ROOT / "policy" / f"{suite}.json")
    except (ValueError, OSError, KeyError):
        return {}
    return {gate["criterion_id"]: gate for gate in policy["gates"]} if found == digest else {}


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


_OPERATORS = {"==": "", ">=": "at least ", ">": "more than ", "<=": "at most ", "<": "less than "}


def _percent(value: str) -> str:
    return f"{float(value) * 100:g}%"


def _threshold_phrase(threshold: dict) -> str:
    """One recorded threshold in words; an unfamiliar metric keeps its own terms."""
    metric, operator, value = threshold["metric"], threshold["operator"], threshold["value"]
    at = _OPERATORS.get(operator)
    exact = operator == "=="
    if at is not None:
        if metric == "unmatched_file_count":
            return ("no" if exact and value == "0" else at + value) + " model files differ from the snapshot manifest"
        if exact and value == "1" and metric in ("health_complete", "warmup_complete", "completion_smoke_complete",
                                                   "required_levels_complete"):
            return {"health_complete": "the service became healthy", "warmup_complete": "warm-up finished",
                    "completion_smoke_complete": "a test completion succeeded",
                    "required_levels_complete": "every required concurrency level completed"}[metric]
        if metric == "exact_match_rate":
            return f"{at}{_percent(value)} of repeated same-boot captures match exactly"
        if metric == "accuracy":
            return f"{at}{_percent(value)} of answers correct"
        if metric == "measured_sample_count":
            return f"{at}{value} questions answered"
        if metric == "duration_seconds" and value.isdigit() and int(value) % 60 == 0:
            return f"{at}{int(value) // 60} minutes of load"
        if metric == "request_error_count" and exact and value == "0":
            return "no request errors"
        if metric == "completed_requests" and operator == ">" and value == "0":
            return "at least one completed request"
        if exact and metric == "dataset_id":
            return f"dataset {value}"
        if exact and threshold["unit"] in ("commit", "digest"):
            return f"{metric.replace('_', ' ')} {value[:12]}"
    return f"{metric} {operator} {value} {threshold['unit']}"


def gate_explanation(thresholds: list[dict], gate: dict | None = None) -> str:
    """What one baseline gate checked, from its recorded thresholds and policy."""
    phrases = [_threshold_phrase(threshold) for threshold in thresholds]
    levels = (gate or {}).get("required_concurrencies")
    if levels:
        phrases = [phrase + f" (concurrency {', '.join(map(str, levels))})"
                   if phrase == "every required concurrency level completed" else phrase for phrase in phrases]
        phrases.append("speed is recorded, not judged")
    text = "; ".join(phrases)
    return text[:1].upper() + text[1:] + "."


def render_policy(policy: dict, digest: str, *, writer: TerminalWriter | None = None) -> None:
    """policy show's human form: each gate and what passing it requires."""
    out = writer or TerminalWriter()
    out.emit(f"Baseline policy {policy['suite']}", role="heading")
    out.field("Digest", digest)
    out.emit("A run passes when every gate passes. Each gate requires:")
    for gate in policy["gates"]:
        out.emit(gate["criterion_id"], "  ", role="choice")
        out.emit(gate_explanation(gate["thresholds"], gate), "    ", "    ")
    overrides = policy.get("accuracy_floor_overrides") or {}
    for model, floor in sorted(overrides.items()):
        out.field("Accuracy floor", f"{_percent(floor)} for {model}")
    out.emit("Full policy (JSON):")
    print(f"./pulsar policy show {policy['suite']} --json", file=out.stream)


def performance_rows(levels: list[dict], width: int) -> list[str]:
    """Benchmark levels as an aligned, rounded table; Requests goes first when narrow."""
    # Measurements record decimals as text, so they are parsed only to round them.
    def number(value, places, unit=""):
        return "—" if value is None else f"{float(value):.{places}f}{unit}"
    header = ["Conc", "Requests", "Total", "Decode", "p95 TTFT"]
    rows = [[str(level["concurrency"]), f"{level['measured_request_count']}/{level['requested_request_count']}",
             number(level["aggregate_tps"], 1), number(level["decode_tps_p50"], 1),
             number(level["ttft_p95_ms"], 0, " ms")] for level in levels]
    widths = [max(len(row[column]) for row in [header, *rows]) for column in range(len(header))]
    columns = list(range(len(header)))
    if 2 + sum(widths) + 2 * (len(widths) - 1) > width:
        columns.remove(1)
    return ["  " + "  ".join(row[column].rjust(widths[column]) for column in columns).rstrip()
            for row in [header, *rows]]


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


def frame_line(report: dict | None, *, now=None) -> str:
    """compact_lines as one value for the recipe frame's Results field."""
    from .catalog import age_seconds, age_text
    if report is None:
        return "not inspected"
    if report.get("historical"):
        return "historical schema-1; see Published results"
    runs, issues = report["runs"], report["issues"]
    if not runs and not issues:
        return "none supplied"
    verified = [run for run in runs if run["status"] == "verified"]
    dated = [run for run in verified if run.get("ended_at")]
    parts = []
    if dated:
        run = dated[0]
        parts.append(f"{run['suite']} {run['outcome']} · {run['ended_at'][:10]} "
                     f"({age_text(age_seconds(run['ended_at'], now))})")
    parts.append(f"{len(runs)} run(s)" + ("" if dated or not verified else ", none with a run time"))
    unavailable = len(runs) - len(verified)
    if unavailable or issues:
        parts.append(f"{unavailable} unverified, {len(issues)} issue(s)")
    return " · ".join(parts) + " (historical)"


def render(report: dict, spec_id: str, *, writer: TerminalWriter | None = None) -> None:
    out = writer or TerminalWriter()
    out.emit(f"Published results for spec {spec_id[:12]}", role="heading")
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
        out.emit("Gates, and what each one checked:")
        thresholds = {entry["criterion_id"]: entry["thresholds"] for entry in run["evaluation"]["measurements"]}
        for criterion, outcome in run["evaluation"]["outcomes"].items():
            out.emit(f"{criterion} {outcome}", "  ")
            if criterion in thresholds:
                out.emit(gate_explanation(thresholds[criterion], run["policy_gates"].get(criterion)), "    ", "    ")
        if run["suite"] == "baseline-v2":
            out.emit("Capture repeatability is diagnostic; differences do not fail baseline-v2.")
        benchmark = run["measurements"].get("benchmark-serving")
        if benchmark:
            levels = benchmark["benchmark-serving"]["levels"]
            out.emit("Performance by concurrency (Conc): Total and Decode in tok/s; Decode is the median per request.")
            for line in performance_rows(levels, out.width):
                print(line, file=out.stream)
            for level in levels:
                if level["completion"] != "complete":
                    out.emit(f"Concurrency {level['concurrency']}: {level['completion']}"
                             + (f"; {level['reason']}" if level.get("reason") else ""), "  ", "    ")
            if any(level[key] is None for level in levels for key in ("aggregate_tps", "decode_tps_p50", "ttft_p95_ms")):
                out.emit("— not recorded", "  ")
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
            out.emit(f"Soak: {float(data['duration_seconds']):.0f} s; concurrency {data['concurrency']}; "
                     f"{data['completed_requests']} completed, {data['request_error_count']} request errors; "
                     f"{soak['completion']}")
        out.emit("Full evidence summary (JSON):")
        # Wrap only between arguments, leaving every identity pasteable.
        arguments = ["./pulsar evidence summary", f"--spec-file releases/{spec_id}.json",
                     "--run " + shlex.quote(run["path"] + "/run.json"), "--evidence-root results --json"]
        print(" \\\n  ".join(arguments), file=out.stream)
