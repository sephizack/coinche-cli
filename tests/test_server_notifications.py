"""Focused tests for server-side Discord table notifications."""

from __future__ import annotations

import asyncio
import json

from coinche import protocol, server


def test_discord_table_notification_posts_expected_embed(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 204

        def __enter__(self) -> Response:
            return self

        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    server._post_discord_table_created("https://discord.example/webhook", "coinche1", "Alice")

    request = captured["request"]
    assert isinstance(request, server.urllib.request.Request)
    assert request.get_method() == "POST"
    assert request.full_url == "https://discord.example/webhook"
    assert captured["timeout"] == server._DISCORD_WEBHOOK_TIMEOUT_SECONDS
    body = json.loads(request.data)
    assert body["username"] == "Coinche"
    assert body["allowed_mentions"] == {"parse": []}
    assert body["embeds"] == [
        {
            "title": "Nouvelle table Coinche",
            "color": server._DISCORD_TABLE_CREATED_COLOR,
            "description": "La table **coinche1** vient d'etre creee par **Alice**.",
        }
    ]


def test_discord_notification_is_scheduled_only_when_table_is_created(monkeypatch) -> None:
    class Writer:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    async def scenario() -> None:
        notifications: list[tuple[str, str, str]] = []

        async def fake_notify(webhook_url: str, table_key: str, player_name: str) -> None:
            notifications.append((webhook_url, table_key, player_name))

        monkeypatch.setattr(server, "_notify_discord_table_created", fake_notify)
        monkeypatch.setattr(server, "DISCORD_NOTIF_CHANNEL_POST_URL", "https://discord.example")
        server.TABLES.clear()
        try:
            for player_name in ("Alice", "Bob"):
                reader = asyncio.StreamReader()
                reader.feed_data(protocol.encode(protocol.JOIN, {"table_key": "coinche1", "player_name": player_name}))
                joined = await server._resolve_join_inner(reader, Writer(), 1000, 0, 0, 0)
                assert joined is not None
                await asyncio.sleep(0)
            assert notifications == [("https://discord.example", "coinche1", "Alice")]
        finally:
            server.TABLES.clear()

    asyncio.run(scenario())
