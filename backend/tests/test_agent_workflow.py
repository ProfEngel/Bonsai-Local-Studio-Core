import asyncio
import json
from pathlib import Path

from backend import server


def test_agent_skill_context_includes_small_data_files(tmp_path: Path) -> None:
    agent_dir = tmp_path / "portfolio-analyst"
    (agent_dir / "chat").mkdir(parents=True)
    (agent_dir / "data").mkdir()
    (agent_dir / "agent.yaml").write_text("id: portfolio-analyst\n")
    (agent_dir / "SKILL.md").write_text("# Skill\n")
    (agent_dir / "data" / "portfolio.md").write_text("# Portfolio-Basis\n- NVIDIA\n")

    context = server._agent_skill_context(agent_dir / "chat" / "profile.json")

    assert "Portfolio-Basis" in context
    assert "NVIDIA" in context


def test_agent_workflow_emits_progress_and_revises_once(monkeypatch) -> None:
    goose_calls: list[list[dict[str, str]]] = []
    judge_calls = 0

    async def fake_goose(**kwargs):
        goose_calls.append(kwargs["messages"])
        return "Erster Entwurf" if len(goose_calls) == 1 else "Überarbeiteter Entwurf"

    async def fake_completion(**kwargs):
        nonlocal judge_calls
        judge_calls += 1
        assert "Antworte ausschließlich als valides JSON" in kwargs["prompt"]
        return '{"verdict":"revise","issues":["Bitte präziser formulieren."]}' if judge_calls == 1 else '{"verdict":"pass","issues":[]}'

    monkeypatch.setattr(server, "_run_goose_harness", fake_goose)
    monkeypatch.setattr(server, "_local_llm_completion", fake_completion)
    request = server.ChatRequest(
        messages=[server.ChatMessage(role="user", content="Bitte formuliere einen Mailrundruf.")],
        agent_id="mailrundruf",
        llm_url="http://127.0.0.1:8081/v1",
        model="Ternary-Bonsai-27B-mlx-2bit",
    )
    profile = server.ChatAgentProfile(
        id="mailrundruf",
        name="Mailrundruf",
        description="Test",
        streamProgress=True,
        systemPrompt="Nur Entwürfe, niemals versenden.",
    )

    async def collect():
        return [json.loads(event.split("data: ", 1)[1]) async for event in server._agent_workflow_events(request, profile, Path("/tmp/profile.json"))]

    events = asyncio.run(collect())

    assert events[-1]["runner"] == "Goose-Harness · geprüfter Agentenworkflow"
    assert "Überarbeiteter Entwurf" in events[-1]["message"]
    assert any(event.get("label") == "Qualitätsprüfung 1/3" for event in events)
    assert any(event.get("label") == "Nachbesserung 1/2" for event in events)
    assert any(event.get("label") == "Qualitätsprüfung 2/3" for event in events)
    assert len(goose_calls) == 2
