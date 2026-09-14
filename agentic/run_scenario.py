import argparse
import json

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
    args = parser.parse_args()

    run = Orchestrator(approval=lambda _: args.approve_release).execute(default_graph(), SCENARIOS[args.scenario])
    print(json.dumps({
        "run_id": run.run_id,
        "status": run.status,
        "state": run.state,
        "metrics": run.metrics,
    }, indent=2))


if __name__ == "__main__":
    main()
