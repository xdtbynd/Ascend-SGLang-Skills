#!/usr/bin/env python3
"""
parse_test_output.py — Parse Docker verification output and produce structured results.

Usage:
    python parse_test_output.py <docker_run.log> <exit_code.txt> <duration.txt> \
        <verification_config.json> [--output verification_result.json]

Reads the raw verification outputs, analyzes them against expected behavior,
compares with the original failure log, and produces a structured result JSON.

Exit codes:
    0 - Parsing completed successfully
    1 - Missing arguments
    2 - Input files not found
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Union

# ── Framework-specific pass/fail patterns ────────────────────────────────────
FRAMEWORK_PATTERNS = {
    "pytest": {
        "pass": re.compile(r"(\d+)\s+passed"),
        "fail": re.compile(r"(\d+)\s+failed"),
        "summary": re.compile(r"=+.*\s+(.+)\s+in\s+[\d.]+s\s+=+"),
    },
    "jest": {
        "pass": re.compile(r"Tests:\s+(\d+)\s+passed"),
        "fail": re.compile(r"Tests:\s+(\d+)\s+failed"),
        "summary": re.compile(r"Test Suites:\s+(.+)"),
    },
    "go": {
        "pass": re.compile(r"---\s+PASS:"),
        "fail": re.compile(r"---\s+FAIL:"),
        "summary": re.compile(r"^(ok|FAIL)\s+"),
    },
    "cargo": {
        "pass": re.compile(r"test result:\s+ok"),
        "fail": re.compile(r"test result:\s+FAILED"),
        "summary": re.compile(r"test result:\s+(.+)\."),
    },
    "ctest": {
        "pass": re.compile(r"(\d+)%\s+tests\s+passed"),
        "fail": re.compile(r"tests\s+failed"),
        "summary": re.compile(r"(\d+)%\s+tests\s+passed"),
    },
    "maven": {
        "pass": re.compile(r"Tests run:\s+(\d+),\s+Failures:\s+0"),
        "fail": re.compile(r"Tests run:\s+(\d+),\s+Failures:\s+(\d+)"),
        "summary": re.compile(r"Tests run:.*"),
    },
    "sglang": {
        # Matches: status=passed, "status":"passed", 'status': "passed", PASSED_COUNT: N, N passed
        "pass": re.compile(
            r'(?:status\s*[:=]\s*"?passed"?|PASSED_COUNT:\s*(\d+)|(\d+)\s+passed)',
            re.IGNORECASE,
        ),
        "fail": re.compile(
            r'(?:status\s*[:=]\s*"?failed"?|FAILED_COUNT:\s*(\d+)|(\d+)\s+failed)',
            re.IGNORECASE,
        ),
        "summary": re.compile(r"(?:PASSED_COUNT|FAILED_COUNT|OVERALL_RESULT)"),
    },
}

# ── Error signature extraction patterns ──────────────────────────────────────
ERROR_SIGNATURE_PATTERNS = [
    re.compile(
        r"(AssertionError|TypeError|ValueError|RuntimeError|AttributeError|"
        r"KeyError|IndexError|ImportError|ModuleNotFoundError|"
        r"ConnectionError|TimeoutError|PermissionError|FileNotFoundError|"
        r"panic|SIGSEGV|SIGABRT|OOM|out of memory|"
        r"Error code|Traceback)",
        re.IGNORECASE,
    ),
    re.compile(r"(FAILED|FAIL\s|Error:|Exception\s+in\s+)", re.IGNORECASE),
]


def load_file(path: str) -> str:
    """Load file contents, returning empty string if missing."""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def parse_exit_code(raw: str) -> Union[int, str]:
    """Parse exit code; return 'TIMEOUT' if applicable."""
    raw = raw.strip()
    if raw.upper() == "TIMEOUT":
        return "TIMEOUT"
    try:
        return int(raw)
    except (ValueError, TypeError):
        return raw


def detect_framework(output: str, config: dict) -> str:
    """Detect the test framework from output or config ecosystem."""
    ecosystem = config.get("ecosystem", "unknown")
    framework_map = {
        "sglang": "sglang",
        "python": "pytest",
        "node": "jest",
        "go": "go",
        "rust": "cargo",
        "cpp": "ctest",
        "java": "maven",
    }
    if ecosystem in framework_map:
        return framework_map[ecosystem]

    # Heuristic detection
    # SGLang test_utils.py / run_tests.py patterns
    if "PASSED_COUNT" in output or "OVERALL_RESULT" in output or "test_utils" in output:
        return "sglang"
    if 'status": "passed"' in output or 'status": "failed"' in output:
        return "sglang"
    if "pytest" in output or "==== test session starts" in output:
        return "pytest"
    if "Tests:" in output and ("passed" in output or "failed" in output):
        return "jest"
    if "--- PASS:" in output or "--- FAIL:" in output:
        return "go"
    if "test result:" in output:
        return "cargo"
    if "tests passed" in output and "%" in output:
        return "ctest"
    if "Tests run:" in output:
        return "maven"

    return "unknown"


def extract_pass_fail(output: str, framework: str) -> Tuple[int, int]:
    """Extract pass/fail counts from test output."""
    patterns = FRAMEWORK_PATTERNS.get(framework)
    if not patterns:
        return (0, 0)

    pass_count = 0
    fail_count = 0

    pass_matches = patterns["pass"].findall(output)
    fail_matches = patterns["fail"].findall(output)

    for m in pass_matches:
        try:
            # findall returns tuple of groups; take first numeric match
            if isinstance(m, tuple):
                count = next((int(x) for x in m if x and x.isdigit()), 1)
            elif isinstance(m, str) and m.isdigit():
                count = int(m)
            else:
                count = 1
            pass_count += count
        except (ValueError, TypeError, StopIteration):
            pass_count += 1

    for m in fail_matches:
        try:
            if isinstance(m, tuple):
                count = next((int(x) for x in m if x and x.isdigit()), 1)
            elif isinstance(m, str) and m.isdigit():
                count = int(m)
            else:
                count = 1
            fail_count += count
        except (ValueError, TypeError, StopIteration):
            fail_count += 1

    # If no matches, do heuristic
    if pass_count == 0 and fail_count == 0:
        if "PASSED" in output or "ok" in output.lower():
            pass_count = 1
        if "FAILED" in output:
            fail_count = 1

    return (pass_count, fail_count)


def check_expected_behavior(output: str, expected: str) -> bool:
    """Check if output matches expected behavior (literal or regex)."""
    if not expected:
        return True

    # Regex mode: /pattern/
    if expected.startswith("/") and expected.endswith("/"):
        pattern = expected[1:-1]
        try:
            return bool(re.search(pattern, output, re.IGNORECASE | re.DOTALL))
        except re.error:
            return expected in output

    # Literal substring match
    return expected in output


def extract_error_signature(failure_log: str) -> Optional[str]:
    """Extract the core error signature from a failure log."""
    if not failure_log:
        return None

    lines = failure_log.strip().split("\n")
    signatures: List[str] = []

    for line in lines[-20:]:  # Focus on the tail end where errors usually are
        for pattern in ERROR_SIGNATURE_PATTERNS:
            match = pattern.search(line)
            if match:
                sig = line.strip()
                if len(sig) > 120:
                    sig = sig[:120] + "..."
                signatures.append(sig)
                break

    # Return the last signature (usually the root cause)
    return signatures[-1] if signatures else None


def check_old_error_gone(output: str, failure_log: str) -> Tuple[bool, List[int]]:
    """Check whether the old error signature appears in the new output."""
    signature = extract_error_signature(failure_log)
    if not signature:
        return (True, [])  # Nothing to compare → assume gone

    # Search for the signature (case-insensitive, allow minor variations)
    search_key = signature[:80]
    match_lines: List[int] = []

    for i, line in enumerate(output.split("\n"), start=1):
        if search_key.lower() in line.lower():
            match_lines.append(i)

    return (len(match_lines) == 0, match_lines)


def determine_verdict(
    exit_code: Union[int, str],
    output_matches_expected: bool,
    old_error_gone: Optional[bool],
) -> str:
    """Determine the verification verdict."""
    if exit_code == "TIMEOUT" or (isinstance(exit_code, int) and exit_code > 127):
        return "TIMEOUT"

    if isinstance(exit_code, int) and exit_code == 0:
        if output_matches_expected:
            return "PASSED"
        else:
            return "UNEXPECTED"

    if isinstance(exit_code, int) and exit_code != 0:
        if old_error_gone:
            return "PARTIAL"
        else:
            return "FIX_INCOMPLETE"

    return "ERROR"


def determine_next_action(verdict: str) -> str:
    """Map verdict to recommended next action."""
    action_map = {
        "PASSED": "ready_to_ship",
        "FIX_INCOMPLETE": "fix_needs_rework",
        "UNEXPECTED": "investigate_unexpected_behavior",
        "PARTIAL": "check_partial_fix",
        "TIMEOUT": "increase_timeout_or_optimize",
        "ERROR": "manual_investigation_needed",
    }
    return action_map.get(verdict, "manual_investigation_needed")


def extract_key_lines(output: str, max_lines: int = 20) -> List[str]:
    """Extract the most relevant lines from the output."""
    lines = output.strip().split("\n")
    key_lines: List[str] = []

    # Always include the last 5 lines (summary)
    tail_start = max(0, len(lines) - 5)
    key_lines.extend(lines[tail_start:])

    # Search for error/fail lines
    for line in lines:
        if len(key_lines) >= max_lines:
            break
        if any(
            kw in line.lower()
            for kw in [
                "error",
                "fail",
                "pass",
                "traceback",
                "exception",
                "assert",
                "====",
                "---",
                "test result",
            ]
        ):
            if line not in key_lines:
                key_lines.append(line)

    # Search for framework summary lines
    for line in lines:
        if len(key_lines) >= max_lines:
            break
        if re.search(r"(passed|failed|ok|FAILED|PASSED)", line):
            if line not in key_lines:
                key_lines.append(line)

    return key_lines[:max_lines]


def generate_verdict_summary(verdict: str, pass_count: int, fail_count: int) -> str:
    """Generate a human-readable verdict summary."""
    summaries = {
        "PASSED": (
            f"The fix is verified. The Docker container test ran successfully "
            f"({pass_count} passed, {fail_count} failed). "
            f"The previously-failing test now passes in a clean environment."
        ),
        "FIX_INCOMPLETE": (
            "The fix did NOT resolve the regression. The old error still appears "
            "in the output. The fix needs rework."
        ),
        "UNEXPECTED": (
            "The test passed but the output does not match expected behavior. "
            "Investigate whether the test is actually testing the right thing."
        ),
        "PARTIAL": (
            f"The old error is gone, but new failures appeared ({fail_count} failed). "
            f"The fix may have introduced a side effect."
        ),
        "TIMEOUT": (
            "The verification timed out. The test may be hanging, or the timeout "
            "needs to be increased."
        ),
        "ERROR": (
            "An unexpected error occurred during verification. Manual investigation needed."
        ),
    }
    return summaries.get(verdict, "Unknown verdict.")


def main():
    if len(sys.argv) < 5:
        print(
            "Usage: python parse_test_output.py <docker_run.log> <exit_code.txt> "
            "<duration.txt> <verification_config.json> [--output result.json]"
        )
        sys.exit(1)

    log_path = sys.argv[1]
    exit_code_path = sys.argv[2]
    duration_path = sys.argv[3]
    config_path = sys.argv[4]

    output_path = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        output_path = sys.argv[idx + 1]

    # Load inputs
    output = load_file(log_path)
    exit_code_raw = load_file(exit_code_path)
    duration_raw = load_file(duration_path)
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

    exit_code = parse_exit_code(exit_code_raw)
    try:
        duration = float(duration_raw.strip())
    except (ValueError, TypeError):
        duration = 0.0

    expected_behavior = config.get("expected_behavior", "")
    failure_log = config.get("failure_log", "")
    docker_command = config.get("resolved_docker_command", "")

    # Detect framework and parse
    framework = detect_framework(output, config)
    pass_count, fail_count = extract_pass_fail(output, framework)

    # Check expectations
    output_matches_expected = check_expected_behavior(output, expected_behavior)

    # Compare old error
    old_error_gone = None
    old_error_details = None
    if failure_log:
        gone, match_lines = check_old_error_gone(output, failure_log)
        old_error_gone = gone
        old_error_details = {
            "old_error_signature": extract_error_signature(failure_log),
            "found_in_output": not gone,
            "match_lines": match_lines,
        }

    # Determine verdict
    verdict = determine_verdict(exit_code, output_matches_expected, old_error_gone)

    # Build issues list
    issues = []
    if verdict != "PASSED":
        if verdict == "FIX_INCOMPLETE":
            issues.append(
                {
                    "severity": "critical",
                    "category": "correctness",
                    "description": "Old error still present in output — fix did not resolve the regression",
                    "expected": "Old error signature absent from output",
                    "actual": "Old error still found in output",
                    "suggestion": "Re-examine the fix and ensure it addresses the root cause identified by test-regression-pr-locator",
                }
            )
        elif verdict == "UNEXPECTED":
            issues.append(
                {
                    "severity": "high",
                    "category": "correctness",
                    "description": "Output does not match expected behavior",
                    "expected": expected_behavior,
                    "actual": f"Output does not contain '{expected_behavior}'",
                    "suggestion": "Check if the test is actually running the correct assertions",
                }
            )
        elif verdict == "PARTIAL":
            issues.append(
                {
                    "severity": "high",
                    "category": "correctness",
                    "description": f"Old error fixed but new failures: {fail_count} failed",
                    "expected": "All tests pass",
                    "actual": f"{fail_count} test(s) failed",
                    "suggestion": "Check for side effects from the fix",
                }
            )
        elif verdict == "TIMEOUT":
            issues.append(
                {
                    "severity": "high",
                    "category": "timeout",
                    "description": "Verification timed out",
                    "expected": "Completion within timeout",
                    "actual": "Container was killed due to timeout",
                    "suggestion": "Increase timeout_seconds or investigate why the test is hanging",
                }
            )

    # Build result
    result = {
        "verdict": verdict,
        "exit_code": exit_code,
        "duration_seconds": duration,
        "output_matches_expected": output_matches_expected,
        "old_error_gone": old_error_gone,
        "key_output_lines": extract_key_lines(output),
        "docker_command": docker_command,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "issues": issues,
        "next_action": determine_next_action(verdict),
    }

    if old_error_details:
        result["old_error_details"] = old_error_details

    # Write result
    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"[OK] Result written to {output_path}")

    # Print summary to stdout
    print("\n=== PARSE SUMMARY ===")
    print(f"Verdict:     {verdict}")
    print(f"Exit code:   {exit_code}")
    print(f"Duration:    {duration:.1f}s")
    print(f"Expected:    {'✓' if output_matches_expected else '✗'}")
    print(
        f"Old error:   {'gone' if old_error_gone else 'still present' if old_error_gone is not None else 'N/A'}"
    )
    print(f"Pass/Fail:   {pass_count}/{fail_count}")
    if issues:
        print(f"Issues:      {len(issues)} found")
    print(f"Next action: {determine_next_action(verdict)}")
    print()


if __name__ == "__main__":
    main()
