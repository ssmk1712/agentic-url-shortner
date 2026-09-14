from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import settings
from .llm import analyze_requirement

Action = Callable[[dict], dict]


@dataclass
class Step:
    name: str
    deps: list[str]
    action: Action
    high_impact: bool = False
    fallback: Action | None = None
    rollback: Action | None = None
    max_retries: int | None = None


@dataclass
class Run:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_run_id: str | None = None
    status: str = "running"
    state: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    step_status: dict[str, str] = field(default_factory=dict)
    step_outputs: dict[str, dict] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)


class Orchestrator:
    """Governed DAG executor for SDLC-style agent workflows.

    Features intentionally demonstrated in executable form:
    - explicit dependency graph and entry gates
    - ready-wave parallelism with synchronization
    - append-only audit events and per-step outputs
    - bounded retry and optional fallback
    - human approval for high-impact actions
    - rollback hooks and safe-stop behavior
    - re-planning by invalidating a changed step and its descendants
    - derived reliability metrics for every run
    """

    def __init__(self, approval: Callable[[str], bool] | None = None):
        self.approval = approval or (lambda _: False)

    def execute(
        self,
        steps: list[Step],
        initial: dict,
        *,
        precompleted: set[str] | None = None,
        parent_run_id: str | None = None,
    ) -> Run:
        self._validate_graph(steps)
        run = Run(state=dict(initial), parent_run_id=parent_run_id)
        pending = {step.name: step for step in steps}
        done = set(precompleted or set())
        completed_for_rollback: list[Step] = []
        started = time.time()

        self._event(run, "orchestrator", "run_started", f"steps={list(pending)}")

        while pending:
            ready = sorted(
                [step for step in pending.values() if set(step.deps) <= done],
                key=lambda step: step.name,
            )
            if not ready:
                self._safe_stop(run, "orchestrator", "dependency graph blocked or cyclic")
                break

            self._event(run, "orchestrator", "wave_started", f"ready={[s.name for s in ready]}")

            approved: list[Step] = []
            for step in ready:
                if step.high_impact and settings.require_human_approval:
                    if not self.approval(step.name):
                        run.step_status[step.name] = "safe_stop"
                        self._safe_stop(run, step.name, "human approval denied or missing")
                        self._rollback(run, completed_for_rollback)
                        self._finish(run, started)
                        self._persist(run)
                        return run
                    self._event(run, step.name, "approval_granted", "human approval checkpoint passed")
                approved.append(step)

            snapshot = dict(run.state)
            results: dict[str, tuple[str, dict | None, str | None, int]] = {}
            workers = max(1, min(settings.parallel_workers, len(approved)))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._run_step, step, snapshot): step for step in approved}
                for future in as_completed(futures):
                    step = futures[future]
                    try:
                        results[step.name] = future.result()
                    except Exception as exc:  # defensive boundary around worker execution
                        results[step.name] = ("failed", None, repr(exc), 0)

            successful_outputs: dict[str, dict] = {}
            failed_step: Step | None = None

            for step in approved:
                status, output, detail, retries = results[step.name]
                for attempt in range(retries):
                    self._event(run, step.name, "retry", f"attempt={attempt + 1}")
                run.step_status[step.name] = status

                if status in {"success", "fallback_success"} and output is not None:
                    successful_outputs[step.name] = output
                    self._event(run, step.name, status, detail or "completed")
                else:
                    failed_step = step
                    self._event(run, step.name, "failed", detail or "step failed")
                    break

            if failed_step:
                self._safe_stop(run, failed_step.name, "step exhausted retries and fallback")
                self._rollback(run, completed_for_rollback)
                self._finish(run, started)
                self._persist(run)
                return run

            try:
                self._merge_wave_outputs(run, successful_outputs)
            except Exception as exc:
                self._event(run, "orchestrator", "merge_failed", repr(exc))
                self._safe_stop(run, "orchestrator", "parallel wave output contract violation")
                self._rollback(run, completed_for_rollback)
                self._finish(run, started)
                self._persist(run)
                return run

            for step in approved:
                run.step_outputs[step.name] = successful_outputs[step.name]
                done.add(step.name)
                completed_for_rollback.append(step)
                pending.pop(step.name, None)

            self._event(run, "orchestrator", "wave_completed", f"completed={[s.name for s in approved]}")

        if run.status == "running":
            run.status = "completed"
        self._finish(run, started)
        self._persist(run)
        return run

    def replan_from(self, previous: Run, steps: list[Step], changed_step: str, state_patch: dict) -> Run:
        """Re-run a changed step and every descendant while preserving unaffected work.

        This models dynamic re-planning without pretending the prototype autonomously edits Git.
        The new run links to the previous run and removes outputs owned by invalidated steps.
        """
        names = {step.name for step in steps}
        if changed_step not in names:
            raise ValueError(f"Unknown step: {changed_step}")

        impacted = self._descendants_including(steps, changed_step)
        retained = names - impacted
        base_state = dict(previous.state)
        for step_name in impacted:
            for key in previous.step_outputs.get(step_name, {}):
                base_state.pop(key, None)
        base_state.pop("latency_ms", None)
        base_state.update(state_patch)

        impacted_steps = [step for step in steps if step.name in impacted]
        run = self.execute(
            impacted_steps,
            base_state,
            precompleted=retained,
            parent_run_id=previous.run_id,
        )
        self._event(run, "orchestrator", "replanned", f"changed={changed_step}; impacted={sorted(impacted)}")
        self._persist(run)
        return run

    def _run_step(self, step: Step, state_snapshot: dict) -> tuple[str, dict | None, str | None, int]:
        max_retries = settings.max_agent_retries if step.max_retries is None else step.max_retries
        last_error: Exception | None = None
        retries = 0

        for attempt in range(max_retries + 1):
            try:
                output = step.action(dict(state_snapshot))
                if not isinstance(output, dict):
                    raise TypeError(f"Step {step.name} must return a dict")
                return "success", output, f"attempt={attempt + 1}", retries
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    retries += 1

        if step.fallback is not None:
            try:
                output = step.fallback(dict(state_snapshot))
                if not isinstance(output, dict):
                    raise TypeError(f"Fallback for {step.name} must return a dict")
                return "fallback_success", output, f"fallback after {retries} retries", retries
            except Exception as fallback_error:
                return "failed", None, f"primary={last_error!r}; fallback={fallback_error!r}", retries

        return "failed", None, repr(last_error), retries

    def _merge_wave_outputs(self, run: Run, outputs: dict[str, dict]) -> None:
        owners: dict[str, str] = {}
        for step_name in sorted(outputs):
            for key in outputs[step_name]:
                if key in owners:
                    raise RuntimeError(
                        f"Parallel output conflict: key '{key}' written by both {owners[key]} and {step_name}"
                    )
                owners[key] = step_name
        for step_name in sorted(outputs):
            run.state.update(outputs[step_name])

    def _rollback(self, run: Run, completed: list[Step]) -> None:
        for step in reversed(completed):
            if step.rollback is None:
                continue
            try:
                step.rollback(dict(run.state))
                self._event(run, step.name, "rollback_success", "compensating action completed")
            except Exception as exc:
                self._event(run, step.name, "rollback_failed", repr(exc))

    def _safe_stop(self, run: Run, step: str, detail: str) -> None:
        run.status = "safe_stopped"
        self._event(run, step, "safe_stop", detail)

    def _finish(self, run: Run, started: float) -> None:
        run.state["latency_ms"] = round((time.time() - started) * 1000, 2)
        terminal = [s for s in run.step_status.values() if s in {"success", "fallback_success", "failed", "safe_stop"}]
        successes = sum(s in {"success", "fallback_success"} for s in terminal)
        run.metrics = {
            "success_rate": round(successes / len(terminal), 4) if terminal else 0.0,
            "retry_count": sum(event["status"] == "retry" for event in run.events),
            "fallback_count": sum(event["status"] == "fallback_success" for event in run.events),
            "rollback_count": sum(event["status"] == "rollback_success" for event in run.events),
            "safe_stop_count": sum(event["status"] == "safe_stop" for event in run.events),
            "end_to_end_latency_ms": run.state["latency_ms"],
        }
        self._event(run, "orchestrator", "run_finished", f"status={run.status}; metrics={run.metrics}")

    def _event(self, run: Run, step: str, status: str, detail: str) -> None:
        run.events.append({"ts": time.time(), "step": step, "status": status, "detail": detail})

    def _persist(self, run: Run) -> None:
        path = Path("artifacts/runs")
        path.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run.run_id,
            "parent_run_id": run.parent_run_id,
            "status": run.status,
            "state": run.state,
            "step_status": run.step_status,
            "step_outputs": run.step_outputs,
            "metrics": run.metrics,
            "events": run.events,
        }
        (path / f"{run.run_id}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _validate_graph(steps: list[Step]) -> None:
        names = [step.name for step in steps]
        if len(names) != len(set(names)):
            raise ValueError("Step names must be unique")
        name_set = set(names)
        for step in steps:
            missing = set(step.deps) - name_set
            # Missing dependencies are allowed only when execute() is used for re-plan subsets.
            # The runtime entry gate will require them in precompleted before the step can run.
            if step.name in step.deps:
                raise ValueError(f"Step {step.name} cannot depend on itself")
            if missing and len(steps) == len(name_set):
                # Kept intentionally permissive for subset re-plans; full execution catches blocked graphs.
                pass

    @staticmethod
    def _descendants_including(steps: list[Step], start: str) -> set[str]:
        impacted = {start}
        changed = True
        while changed:
            changed = False
            for step in steps:
                if step.name not in impacted and set(step.deps) & impacted:
                    impacted.add(step.name)
                    changed = True
        return impacted


def requirements_agent(state: dict) -> dict:
    requirement = state["requirement"]
    if state.get("ambiguous"):
        analysis = analyze_requirement(requirement)
    else:
        analysis = {
            "intent": requirement,
            "assumptions": ["Prototype is single-region and locally runnable"],
            "acceptance_criteria": ["Runnable service", "Tests pass", "Release requires approval"],
        }
    return {"requirement_analysis": analysis}


def requirements_fallback(state: dict) -> dict:
    return {
        "requirement_analysis": {
            "intent": state["requirement"],
            "assumptions": ["LLM unavailable; human review required for unresolved ambiguity"],
            "acceptance_criteria": ["Do not auto-resolve ambiguous scope"],
            "risks": ["Requirement ambiguity remains unresolved"],
        },
        "needs_human_clarification": True,
    }


def policy_agent(state: dict) -> dict:
    requirement = str(state.get("requirement", "")).strip()
    if not requirement:
        raise ValueError("Requirement cannot be empty")
    if state.get("needs_human_clarification"):
        raise ValueError("Unresolved requirement ambiguity requires human clarification before downstream execution")
    return {
        "policy_guardrails": {
            "secrets": "environment only; never persisted to run artifacts",
            "release": "human approval required when configured",
            "change_control": "high-impact actions are gated",
            "url_security": "only http/https targets accepted by product plane",
        }
    }


def architecture_agent(state: dict) -> dict:
    return {
        "architecture": {
            "product_plane": "FastAPI -> service -> repository -> SQLite; redirect records analytics",
            "control_plane": "Governed DAG with dependency waves, retries, fallback, audit lineage, re-plan and approval gate",
            "llm_boundary": "GPT-4o is used only for ambiguous/cognitive requirement analysis",
        }
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def implementation_agent(state: dict) -> dict:
    root = _repo_root()
    modules = [
        "app/main.py",
        "app/service.py",
        "app/repository.py",
        "app/db.py",
        "agentic/orchestrator.py",
        "agentic/llm.py",
    ]
    missing = [module for module in modules if not (root / module).exists()]
    if missing:
        raise RuntimeError(f"Implementation artifacts missing: {missing}")

    hashes = {
        module: hashlib.sha256((root / module).read_bytes()).hexdigest()
        for module in modules
    }
    return {
        "implementation": {
            "status": "verified",
            "modules": modules,
            "sha256": hashes,
        }
    }


def test_agent(state: dict) -> dict:
    root = _repo_root()
    command = [sys.executable, "-m", "pytest", "-q", "tests/test_api.py"]
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(f"Product validation failed: {output[-2000:]}")
    return {
        "validation": {
            "passed": True,
            "command": "python -m pytest -q tests/test_api.py",
            "summary": output[-1000:],
        }
    }


def docs_agent(state: dict) -> dict:
    root = _repo_root()
    docs = [
        "ENGINEERING_WALKTHROUGH.md",
        "README.md",
        "docs/WHITEBOARD.md",
        "docs/ARCHITECTURE.md",
        "docs/DECISIONS.md",
        "examples/SCENARIOS.md",
    ]
    checks = {}
    for relative in docs:
        path = root / relative
        if not path.exists():
            raise RuntimeError(f"Documentation artifact missing: {relative}")
        text = path.read_text(encoding="utf-8")
        if len(text.strip()) < 500:
            raise RuntimeError(f"Documentation artifact is unexpectedly thin: {relative}")
        checks[relative] = {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    walkthrough = (root / "ENGINEERING_WALKTHROUGH.md").read_text(encoding="utf-8")
    if "```mermaid" not in walkthrough:
        raise RuntimeError("Primary walkthrough must include architecture/workflow diagrams")

    return {
        "documentation": {
            "passed": True,
            "primary": "ENGINEERING_WALKTHROUGH.md",
            "artifacts": checks,
        }
    }


def release_agent(state: dict) -> dict:
    if not state.get("validation", {}).get("passed"):
        raise RuntimeError("Release gate cannot pass without successful product validation")
    if not state.get("documentation", {}).get("passed"):
        raise RuntimeError("Release gate cannot pass without documentation validation")
    return {"release_ready": True}


def default_graph() -> list[Step]:
    return [
        Step("requirements", [], requirements_agent, fallback=requirements_fallback),
        Step("policy", ["requirements"], policy_agent, max_retries=0),
        Step("architecture", ["requirements", "policy"], architecture_agent, max_retries=0),
        Step("implementation", ["architecture"], implementation_agent, max_retries=0),
        Step("tests", ["implementation"], test_agent, max_retries=1),
        Step("docs", ["implementation"], docs_agent, max_retries=0),
        Step("release", ["tests", "docs"], release_agent, high_impact=True, max_retries=0),
    ]
