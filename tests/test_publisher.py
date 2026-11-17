from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx

from alert_enrichment.errors import PublishError
from alert_enrichment.slack import RenderedMessage, SinkPublisher, SlackPublisher

URL = "https://hooks.slack.test/services/T000/B000/XXXX"
MESSAGE = RenderedMessage(
    text="hello",
    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
)


def record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("alert_enrichment.slack.publisher.asyncio.sleep", fake_sleep)
    return sleeps


async def test_publish_posts_text_and_blocks() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(URL).mock(return_value=httpx.Response(200, text="ok"))
        publisher = SlackPublisher(URL)
        await publisher.publish(MESSAGE)
        await publisher.aclose()

    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["text"] == "hello"
    assert body["blocks"][0]["type"] == "section"


async def test_429_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = record_sleeps(monkeypatch)
    with respx.mock() as router:
        router.post(URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, text="ok"),
            ]
        )
        publisher = SlackPublisher(URL)
        await publisher.publish(MESSAGE)
        await publisher.aclose()

    assert sleeps == [2.0]


async def test_4xx_fails_fast_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = record_sleeps(monkeypatch)
    with respx.mock() as router:
        router.post(URL).mock(return_value=httpx.Response(400, text="invalid_payload"))
        publisher = SlackPublisher(URL)
        with pytest.raises(PublishError, match="400"):
            await publisher.publish(MESSAGE)
        await publisher.aclose()

    assert sleeps == []


async def test_5xx_is_retried_then_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = record_sleeps(monkeypatch)
    with respx.mock() as router:
        router.post(URL).mock(return_value=httpx.Response(503, text="upstream down"))
        publisher = SlackPublisher(URL, max_retries=1)
        with pytest.raises(PublishError, match="503"):
            await publisher.publish(MESSAGE)
        await publisher.aclose()

    assert len(sleeps) == 1


async def test_transport_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    record_sleeps(monkeypatch)
    with respx.mock() as router:
        router.post(URL).mock(side_effect=httpx.ConnectError("dns boom"))
        publisher = SlackPublisher(URL, max_retries=0)
        with pytest.raises(PublishError, match="transport error"):
            await publisher.publish(MESSAGE)
        await publisher.aclose()


async def test_sink_writes_exact_payload_as_json_lines(tmp_path: Path) -> None:
    sink = SinkPublisher(tmp_path / "slack.jsonl")
    await sink.publish(MESSAGE)
    await sink.publish(MESSAGE)
    await sink.aclose()

    lines = (tmp_path / "slack.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    body = json.loads(lines[0])
    assert body["text"] == "hello"
    assert body["blocks"][0]["type"] == "section"


def test_retry_after_parsing() -> None:
    from alert_enrichment.slack.publisher import _retry_after_seconds

    parser: Callable[[str | None], float] = _retry_after_seconds
    assert parser("3") == 3.0
    assert parser(None) == 1.0
    assert parser("not-a-number") == 1.0
    assert parser("0") == 0.5
