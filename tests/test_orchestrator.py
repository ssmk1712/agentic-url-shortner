import os

os.environ["REQUIRE_HUMAN_APPROVAL"] = "true"
os.environ["MAX_AGENT_RETRIES"] = "2"

import agentic.orchestrator as orchestrator_module
from agentic.orchestrator import Orchestrator, Step, default_graph, docs_agent


def _deterministic_default_graph():
    """Default topology with in-memory leaf actions for orchestration unit tests.

    The real scenario path still uses implementation_agent/test_agent/docs_agent. Unit tests
    should not recursively launch a second pytest process through test_agent.
    """

    def implementation(_state):
        return {"implementation": {"status": "verified", "source": "unit-test-fixture"}}

    def validate(_state):
        return {"validation": {"passed": True, "source": "unit-test-fixture"}}

    def docs(_state):
        return {"documentation": {"passed": True, "source": "unit-test-fixture"}}

    return default_graph(
        implementation_action=implementation,
        test_action=validate,
        documentation_action=docs,
    )


def test_safe_stop_without_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = Orchestrator().execute(_deterministic_default_graph(), {"requirement": "x", "ambiguous": False})
    assert run.status == "safe_stopped"
    assert not run.state.get("release_ready")
    assert run.step_status["release"] == "safe_stop"
    assert run.state["safe_stop"]["step"] == "release"
    assert "approval" in run.state["safe_stop"]["reason"]
    assert run.metrics["safe_stop_count"] >= 1


def test_release_with_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = Orchestrator(lambda _: True).execute(
        _deterministic_default_graph(), {"requirement": "x", "ambiguous": False}
    )
    assert run.status == "completed"
    assert run.state["release_ready"] is True
    assert run.step_status["tests"] == "success"
    assert run.step_status["docs"] == "success"
    assert run.metrics["success_rate"] == 1.0
    assert run.metrics["retry_frequency"] == 0.0
    assert run.metrics["rollback_frequency"] == 0.0
    assert run.metrics["mttr_ms"] is None
    assert (tmp_path / "artifacts" / "runs" / f"{run.run_id}.json").exists()


def test_retry_then_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    attempts = {"count": 0}

    def flaky(_state):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise RuntimeError("temporary")
        return {"ok": True}

    run = Orchestrator(lambda _: True).execute([Step("flaky", [], flaky)], {"requirement": "x"})
    assert run.status == "completed"
    assert run.state["ok"] is True
    assert run.metrics["retry_count"] == 1
    assert run.metrics["retry_frequency"] == 1.0
    assert run.metrics["mttr_ms"] is not None
    assert run.metrics["mttr_ms"] >= 0.0


def test_fallback_after_retry_exhaustion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def always_fail(_state):
        raise RuntimeError("primary unavailable")

    def fallback(_state):
        return {"degraded": True}

    run = Orchestrator().execute(
        [Step("cognitive", [], always_fail, fallback=fallback, max_retries=1)],
        {"requirement": "x"},
    )
    assert run.status == "completed"
    assert run.state["degraded"] is True
    assert run.step_status["cognitive"] == "fallback_success"
    assert run.metrics["fallback_count"] == 1


def test_rollback_runs_on_downstream_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rolled_back = {"value": False}

    def create(_state):
        return {"resource": "created"}

    def rollback(_state):
        rolled_back["value"] = True
        return {}

    def fail(_state):
        raise RuntimeError("fatal")

    steps = [
        Step("create", [], create, rollback=rollback),
        Step("fail", ["create"], fail, max_retries=0),
    ]
    run = Orchestrator().execute(steps, {"requirement": "x"})
    assert run.status == "safe_stopped"
    assert rolled_back["value"] is True
    assert run.metrics["rollback_count"] == 1
    assert run.metrics["rollback_frequency"] == 0.5


def test_replan_invalidates_changed_step_and_descendants(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    steps = [
        Step("requirements", [], lambda s: {"normalized": s["requirement"].upper()}),
        Step("architecture", ["requirements"], lambda s: {"design": f"design:{s['normalized']}"}),
        Step("docs", ["architecture"], lambda s: {"doc": f"doc:{s['design']}"}),
    ]
    orchestrator = Orchestrator()
    first = orchestrator.execute(steps, {"requirement": "v1"})
    second = orchestrator.replan_from(first, steps, "requirements", {"requirement": "v2"})

    assert second.parent_run_id == first.run_id
    assert second.state["normalized"] == "V2"
    assert second.state["design"] == "design:V2"
    assert second.state["doc"] == "doc:design:V2"


def test_ambiguous_requirement_without_llm_key_safe_stops_for_clarification(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    run = Orchestrator(lambda _: True).execute(
        default_graph(), {"requirement": "Make it enterprise ready and smarter", "ambiguous": True}
    )
    assert run.status == "safe_stopped"
    assert run.state["needs_human_clarification"] is True
    assert run.step_status["requirements"] == "fallback_success"
    assert run.step_status["policy"] == "failed"
    assert "release_ready" not in run.state

def test_safe_stop_includes_underlying_step_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def fail(_state):
        raise RuntimeError("specific validation failure")

    run = Orchestrator().execute(
        [Step("validate", [], fail, max_retries=0)],
        {"requirement": "x"},
    )

    assert run.status == "safe_stopped"
    assert run.state["safe_stop"]["step"] == "validate"
    assert "specific validation failure" in run.state["safe_stop"]["reason"]


def test_docs_agent_requires_primary_contract_but_not_supplemental_docs(tmp_path, monkeypatch):
    walkthrough = (
        "# Engineering Walkthrough\n\n"
        "```mermaid\ngraph TD\nA-->B\n```\n\n"
        "Architecture, testing, and risk are described here.\n"
        + ("Detailed engineering context. " * 220)
    )
    readme = "# Project\n\n" + ("Setup and execution guidance. " * 30)
    (tmp_path / "ENGINEERING_WALKTHROUGH.md").write_text(walkthrough, encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    monkeypatch.setattr(orchestrator_module, "_repo_root", lambda: tmp_path)

    output = docs_agent({})["documentation"]

    assert output["passed"] is True
    assert output["artifacts"]["ENGINEERING_WALKTHROUGH.md"]["required"] is True
    assert output["artifacts"]["docs/ARCHITECTURE.md"]["present"] is False
    assert any("docs/ARCHITECTURE.md" in warning for warning in output["warnings"])

def test_parallel_wave_records_all_sibling_results_before_safe_stop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def succeed(_state):
        return {"sibling_output": True}

    def fail(_state):
        raise RuntimeError("parallel branch failed")

    run = Orchestrator().execute(
        [
            Step("a_fail", [], fail, max_retries=0),
            Step("b_success", [], succeed, max_retries=0),
        ],
        {"requirement": "x"},
    )

    assert run.status == "safe_stopped"
    assert run.step_status["a_fail"] == "failed"
    assert run.step_status["b_success"] == "success"
    assert any(
        event["step"] == "b_success" and event["status"] == "success"
        for event in run.events
    )
    assert "parallel branch failed" in run.state["safe_stop"]["reason"]

