import asyncio
import json

from backend import server


def test_news_briefing_emits_progress_and_bounded_result(monkeypatch) -> None:
    async def fake_search(query: str, provider: str):
        section = next(name for name, candidate in server.NEWS_BRIEFING_SECTIONS if candidate == query)
        return ([{
            "title": f"{section} aktuell",
            "url": f"https://example.test/{section}",
            "snippet": f"Belastbare Meldung für {section}.",
            "provider": "Test",
        }], query, "2026-07-15 12:00 CEST", "Test")

    calls: list[str] = []

    async def fake_completion(**kwargs):
        calls.append(kwargs["prompt"])
        if "ausschließlich als JSON" in kwargs["prompt"]:
            return '{"verdict":"pass","issues":[]}'
        return "## Welt\nMeldung [1]\n\n## Deutschland\nMeldung [2]\n\n## Region Stuttgart\nMeldung [3]\n\n## Wirtschaft & Finanzen\nMeldung [4]\n\n## Sport\nMeldung [5]\n\n## HfWU\nMeldung [6]"

    monkeypatch.setattr(server, "_web_search", fake_search)
    monkeypatch.setattr(server, "_local_llm_completion", fake_completion)
    request = server.ChatRequest(
        messages=[server.ChatMessage(role="user", content="Erstelle das News-Briefing.")],
        web_search=True,
        web_search_provider="auto",
        agent_id=server.NEWS_BRIEFING_AGENT_ID,
        llm_url="http://127.0.0.1:8081/v1",
        model="Ternary-Bonsai-27B-mlx-2bit",
    )
    profile = server.ChatAgentProfile(
        id=server.NEWS_BRIEFING_AGENT_ID,
        name="News-Briefing",
        description="Test",
        webSearchDefault=True,
        streamProgress=True,
        systemPrompt="Test",
    )

    async def collect():
        return [json.loads(event.split("data: ", 1)[1]) async for event in server._news_briefing_events(request, profile)]

    events = asyncio.run(collect())
    assert events[-1]["runner"] == "Lokaler News-Workflow · Bonsai-27B"
    assert "### Quellen" in events[-1]["message"]
    assert any(event.get("label") == "Zeitstempel erfasst" for event in events)
    assert sum("ausschließlich als JSON" in call for call in calls) == 1
