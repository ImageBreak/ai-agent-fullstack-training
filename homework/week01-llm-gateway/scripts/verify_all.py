"""Run the actual pytest suite and produce a redacted acceptance report."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GROUPS = (
    "routing",
    "streaming",
    "structured",
    "prompts",
    "observability",
    "resilience",
    "limits",
    "security",
)


class Reporter:
    def __init__(self):
        self.groups = {}
        self.results = {}

    def pytest_collection_modifyitems(self, items):
        for item in items:
            self.groups[item.nodeid] = [name for name in (*GROUPS, "live") if item.get_closest_marker(name)]

    def pytest_runtest_logreport(self, report):
        current = self.results.setdefault(
            report.nodeid,
            {
                "test": report.nodeid,
                "groups": self.groups.get(report.nodeid, []),
                "status": "NOT_RUN",
                "evidence": {},
            },
        )
        if report.failed:
            current["status"] = "FAIL"
            current["failure_phase"] = report.when
        elif report.skipped and current["status"] != "FAIL":
            current["status"] = "SKIP"
        elif report.when == "call" and current["status"] != "FAIL":
            current["status"] = "PASS"
        current["evidence"].update(dict(report.user_properties))

    def report(self, live, exit_code):
        groups = {}
        for name in (*GROUPS, "live"):
            cases = [case for case in self.results.values() if name in case["groups"]]
            status = (
                "FAIL"
                if any(case["status"] == "FAIL" for case in cases)
                else "PASS"
                if cases and all(case["status"] == "PASS" for case in cases)
                else "NOT_RUN"
            )
            if name == "live" and not live:
                status = "NOT_RUN"
            groups[name] = {
                "status": status,
                "cases": len(cases),
                "passed": sum(case["status"] == "PASS" for case in cases),
            }
        required = (*GROUPS, "live") if live else GROUPS
        success = exit_code == 0 and all(groups[name]["status"] == "PASS" for name in required)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "suite_passed": success,
            "acceptance_complete": success and live,
            "scope": "Mock, local HTTP and real models" if live else "Mock and local HTTP only",
            "groups": groups,
            "tests": list(self.results.values()),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Explicitly permit billable upstream calls.")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "verification.json")
    args = parser.parse_args()
    if args.live:
        print("LIVE enabled: real model requests may incur charges; missing credentials fail validation.")
    reporter = Reporter()
    arguments = ["-q", str(ROOT / "tests")]
    if args.live:
        arguments.append("--live")
    else:
        arguments.extend(["-m", "not live"])
    result = pytest.main(arguments, plugins=[reporter])
    report = reporter.report(args.live, int(result))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, group in report["groups"].items():
        print(f"[{group['status']}] {name}: {group['passed']}/{group['cases']}")
    print(f"Report: {args.report}")
    return 0 if report["suite_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
