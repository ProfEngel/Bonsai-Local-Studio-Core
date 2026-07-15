from backend import server


def test_extract_portfolio_visual_keeps_only_bounded_declarative_data() -> None:
    answer = '''Einordnung mit Quelle [1].

<portfolio_visual>{"kpis":[{"label":"DAX","value":"20.000 Punkte","change":"+1,2 %","tone":"positive","note":"Tagesbewegung"}],"chart":{"title":"Markttreiber","unit":"%","items":[{"label":"DAX","value":1.2},{"label":"Bitcoin","value":-2.4}]}}</portfolio_visual>'''

    text, visual = server._extract_portfolio_visual(answer)

    assert text == "Einordnung mit Quelle [1]."
    assert visual is not None
    assert visual["kpis"][0]["label"] == "DAX"
    assert visual["chart"]["items"][1]["value"] == -2.4


def test_extract_portfolio_visual_drops_invalid_chart_values() -> None:
    answer = '<portfolio_visual>{"chart":{"items":[{"label":"Ungültig","value":"kein Wert"}]}}</portfolio_visual>'

    text, visual = server._extract_portfolio_visual(answer)

    assert text == ""
    assert visual is None

