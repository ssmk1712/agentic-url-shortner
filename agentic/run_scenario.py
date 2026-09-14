import argparse
import json
import sys

from .orchestrator import Orchestrator, default_graph

SCENARIOS = {
    "greenfield": {
        "requirement": "Build URL shortening, redirect, analytics, expiry, tests and documentation.",
        "ambiguous": False,
    },
    "brownfield": {
        "requirement": "Enhance the existing URL shortener with custom aliases without breaking existing redirects and analytics.",
        "ambiguous": False,
    },
    "ambiguous": {
        "requirement": "Make shortened links enterprise ready and smarter.",
        "ambiguous": True,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a governed SDLC orchestration scenario")
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--approve-release", action="store_true", help="Pass the high-impact human release gate")
    parser.add_argument(
        "--expected-status",
        choices=("completed", "safe_stopped"),
        default="completed",
        help=(
            "Expected terminal status. The command exits non-zero when the actual status differs, "
            "which makes scenario execution useful in CI."
        ),
    )
    args = parser.parse_args()

    run = Orchestrator(approval=lambda _: args.approve_release).execute(
        default_graph(), SCENARIOS[args.scenario]
    )
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "status": run.status,
                "state": run.state,
                "step_status": run.step_status,
                "metrics": run.metrics,
            },
            indent=2,
        )
    )

    if run.status != args.expected_status:
        print(
            f"Scenario status mismatch: expected={args.expected_status} actual={run.status}",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
