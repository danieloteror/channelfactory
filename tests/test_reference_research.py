"""Análisis de referencia sin red: qué funcionó (determinista) y catálogo vía yt-dlp simulado."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from channel_factory.reference_research import analyze, cohorts, patterns


def _rows(n: int = 24) -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for i in range(n):
        q = i % 3 == 0  # un tercio con títulos-pregunta que rinden más
        out.append({"video_id": f"v{i}", "format": "long", "duration_s": 600 + 10 * i,
                    "title": (f"Why does money lose value {i}?" if q else f"Money update number {i}"),
                    "published_at": (now - timedelta(days=40 + i)).isoformat(),
                    "views": (90000 if q else 10000) + i * 100})
    return cohorts.score(out)


def test_what_worked_finds_title_pattern() -> None:
    ww = patterns.what_worked(_rows())
    g = ww["by_format"]["long"]
    feats = {f["rasgo"]: f for f in g["rasgos_de_titulo"]}
    assert feats["titulo_pregunta"]["top"] > feats["titulo_pregunta"]["resto"]
    assert all(v["title"].startswith("Why") for v in g["top_videos"][:5])
    assert any("money" in p for p, _ in ww["title_formulas"])


def test_small_samples_are_flagged() -> None:
    ww = patterns.what_worked(_rows(4))
    assert "insuficiente" in ww["by_format"]["long"]["note"]


def test_catalog_via_ytdlp_uses_tabs_without_api_key(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    tabs = {
        "videos": {"channel": "Ref", "channel_id": "UC123", "channel_follower_count": 1000,
                   "entries": [{"id": f"L{i}", "title": f"Why thing {i}?", "duration": 600, "view_count": 1000 * i}
                               for i in range(1, 8)]},
        "shorts": {"entries": [{"id": "S1", "title": "Short", "duration": 40, "view_count": 5000}]},
        "streams": {"entries": []},
    }
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(analyze.ytdlp, "playlist_json", lambda url, limit: tabs[url.rsplit("/", 1)[1]])
    monkeypatch.setattr(analyze.ytdlp, "metadata", lambda url: {"upload_date": "20260101", "like_count": 10,
                                                                 "comment_count": 2, "view_count": 777})
    monkeypatch.setattr(analyze.ytdlp, "subtitles", lambda url, d: [])
    monkeypatch.setattr(analyze, "_save_thumb", lambda url, dest: None)
    res = analyze.analyze_channel("https://youtube.com/@ref/videos?si=x", tmp_path, enrich=10)
    fmts = {v["video_id"]: v["format"] for v in res["videos"]}
    assert fmts["S1"] == "short" and fmts["L1"] == "long"
    assert res["channel"]["channel_id"] == "UC123" and res["channel"]["subscribers"] == 1000
    assert (tmp_path / "reference_report.md").read_text().count("Qué le funcionó") == 1
    assert any("yt-dlp" in x for x in res["access_limitations"])
