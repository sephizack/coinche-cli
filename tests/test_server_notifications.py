"""Focused tests for server-side Discord table notifications."""

from __future__ import annotations

import asyncio
import json

from coinche import protocol, server
from coinche.cards import Seat


def test_table_key_pattern_allows_twenty_alphanumeric_characters():
    assert server.TABLE_KEY_PATTERN.fullmatch("A" * 20)
    assert not server.TABLE_KEY_PATTERN.fullmatch("A" * 21)


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
    monkeypatch.setattr(server, "COINCHE_PUBLIC_URL", "https://coinche.example.org")

    server._post_discord_table_created("https://discord.example/webhook", "coinche1", "Alice", Seat.N)

    request = captured["request"]
    assert isinstance(request, server.urllib.request.Request)
    assert request.get_method() == "POST"
    assert request.full_url == "https://discord.example/webhook?with_components=true"
    assert captured["timeout"] == server._DISCORD_WEBHOOK_TIMEOUT_SECONDS
    body = json.loads(request.data)
    assert body["username"] == "Coinche CLI"
    assert body["allowed_mentions"] == {"parse": []}
    assert body["embeds"] == [
        {
            "title": "Nouvelle table !",
            "color": server._DISCORD_TABLE_CREATED_COLOR,
            "description": "La table **coinche1** vient d'etre creee par **Alice**.",
        }
    ]
    assert body["components"] == [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 5,
                    "label": "Avec Alice",
                    "emoji": {"name": "🤝"},
                    "url": "https://coinche.example.org/?table=coinche1&seat=S",
                },
                {
                    "type": 2,
                    "style": 5,
                    "label": "Contre Alice",
                    "emoji": {"name": "⚔️"},
                    "url": "https://coinche.example.org/?table=coinche1&seat=W",
                },
                {
                    "type": 2,
                    "style": 5,
                    "label": "Regarder la partie",
                    "emoji": {"name": "👁️"},
                    "url": "https://coinche.example.org/?table=coinche1&spectate=true",
                },
            ],
        }
    ]


def test_discord_notification_is_scheduled_only_for_notified_new_tables(monkeypatch) -> None:
    class Writer:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    async def scenario() -> None:
        notifications: list[tuple[str, str, str, Seat]] = []

        async def fake_notify(webhook_url: str, table_key: str, player_name: str, creator_seat: Seat) -> None:
            notifications.append((webhook_url, table_key, player_name, creator_seat))

        monkeypatch.setattr(server, "_notify_discord_table_created", fake_notify)
        monkeypatch.setattr(server, "DISCORD_NOTIF_CHANNEL_POST_URL", "https://discord.example")
        server.TABLES.clear()
        try:
            for table_key, player_name, suppress_notification in (
                ("Noctis", "Alice", True),
                ("CosmoCanyon", "Bob", False),
                ("cosmocanyon", "Charlie", False),
            ):
                reader = asyncio.StreamReader()
                reader.feed_data(
                    protocol.encode(
                        protocol.JOIN,
                        {
                            "table_key": table_key,
                            "player_name": player_name,
                            "suppress_discord_notification": suppress_notification,
                        },
                    )
                )
                joined = await server._resolve_join_inner(reader, Writer(), 1000, 0, 0, 0)
                assert joined is not None
                await asyncio.sleep(0)
            assert notifications == [("https://discord.example", "CosmoCanyon", "Bob", Seat.N)]
        finally:
            server.TABLES.clear()

    asyncio.run(scenario())
