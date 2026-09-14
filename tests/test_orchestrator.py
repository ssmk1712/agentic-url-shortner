import os

os.environ["REQUIRE_HUMAN_APPROVAL"] = "true"
os.environ["MAX_AGENT_RETRIES"] = "2"

from agentic.orchestrator import Orchestrator, Step, default_graph


def test_safe_stop_without_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = Orchestrator().execute(default_graph(), {"requirement": "x", "ambiguous": False})
    assert run.status == "safe_stopped"
    assert not run.state.get("release_ready")
    assert run.step_status["release"] == "safe_stop"
    assert run.metrics["safe_stop_count"] >= 1


def test_release_with_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = Orchestrator(lambda _: True).execute(
        default_graph(), {"requirement": "x", "ambiguous": False}
    )
    assert run.status == "completed"
    assert run.state["release_ready"] is True
    assert run.step_status["tests"] == "success"
    assert run.step_status["docs"] == "success"
    assert run.metrics["success_rate"] == 1.0
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
