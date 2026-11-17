from __future__ import annotations

from alert_enrichment.__main__ import main


def test_main_runs_uvicorn_with_the_app_factory(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    def fake_run(target: str, **kwargs: object) -> None:
        captured.update({"target": target, **kwargs})

    def fake_configure(level: str, json_logs: bool) -> None:
        captured["logging"] = (level, json_logs)

    monkeypatch.setattr("alert_enrichment.__main__.uvicorn.run", fake_run)
    monkeypatch.setattr("alert_enrichment.__main__.configure_logging", fake_configure)

    main()

    assert captured["target"] == "alert_enrichment.main:app"
    assert captured["port"] == 8080
    assert captured["host"] == "0.0.0.0"
    assert captured["logging"] == ("INFO", True)
