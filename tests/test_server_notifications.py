"""Focused tests for server-side Discord table notifications."""

from __future__ import annotations

import asyncio
import json

from coinche import protocol, server
from coinche.cards import Seat
from coinche.table import Table


def test_table_key_pattern_allows_twenty_alphanumeric_characters():
    assert server.TABLE_KEY_PATTERN.fullmatch("A" * 20)
    assert not server.TABLE_KEY_PATTERN.fullmatch("A" * 21)


def test_set_bot_type_changes_one_bot_and_broadcasts_confirmation() -> None:
    class Writer:
        def __init__(self) -> None:
            self.written: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.written.append(data)

        async def drain(self) -> None:
            return None

    async def scenario() -> None:
        writer = Writer()
        table = Table("bots", bot_type="noob")
        table.add_player("Alice", writer)
        table.fill_with_bots()

        await server._dispatch(table, Seat.N, protocol.SET_BOT_TYPE, {"seat": "E", "bot_type": "maestro"})

        assert table.seats[Seat.E].bot_type == "maestro"
        message_type, payload = protocol.decode(writer.written[-1])
        assert message_type == protocol.BOT_TYPE_CHANGED
        assert payload == {"seat": "E", "bot_type": "maestro"}

    asyncio.run(scenario())


def test_discord_table_notification_posts_expected_embed(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"id": "msg_42"}).encode("utf-8")

    def fake_urlopen(request: object, timeout: float) -> Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(server, "COINCHE_PUBLIC_URL", "https://coinche.example.org")

    msg_id = server._post_discord_table_created("https://discord.example/webhook", "coinche1", "Alice", Seat.N)
    assert msg_id == "msg_42"

    request = captured["request"]
    assert isinstance(request, server.urllib.request.Request)
    assert request.get_method() == "POST"
    assert request.full_url == "https://discord.example/webhook?with_components=true&wait=true"
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


def test_discord_table_closed_patches_expected_embed(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    server._patch_discord_table_closed("https://discord.example/webhook?with_components=true", "coinche1", "msg_42")

    request = captured["request"]
    assert isinstance(request, server.urllib.request.Request)
    assert request.get_method() == "PATCH"
    assert request.full_url == "https://discord.example/webhook/messages/msg_42?with_components=true"
    assert captured["timeout"] == server._DISCORD_WEBHOOK_TIMEOUT_SECONDS
    body = json.loads(request.data)
    assert body["embeds"] == [
        {
            "title": "🔒 [Fermée] Table coinche1",
            "color": server._DISCORD_TABLE_CLOSED_COLOR,
            "description": "La table **coinche1** est fermée.",
        }
    ]
    assert body["components"] == []


def test_discord_notification_is_scheduled_only_for_notified_new_tables(monkeypatch) -> None:
    class Writer:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

    async def scenario() -> None:
        notifications: list[tuple[str, str, str, Seat]] = []

        async def fake_notify(webhook_url: str, table: Table, player_name: str, creator_seat: Seat) -> None:
            notifications.append((webhook_url, table.table_key, player_name, creator_seat))

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


def test_table_removal_schedules_discord_close_notification(monkeypatch) -> None:
    closed_notifications: list[tuple[str, str, str]] = []

    async def fake_notify_closed(webhook_url: str, table_key: str, message_id: str) -> None:
        closed_notifications.append((webhook_url, table_key, message_id))

    monkeypatch.setattr(server, "_notify_discord_table_closed", fake_notify_closed)
    monkeypatch.setattr(server, "DISCORD_NOTIF_CHANNEL_POST_URL", "https://discord.example")

    async def scenario() -> None:
        table = Table("test_close")
        table.discord_message_id = "msg_999"
        server.TABLES["test_close"] = table

        await server._remove_table_and_notify(table)

        assert "test_close" not in server.TABLES
        assert table.is_closed
        await asyncio.sleep(0)
        assert closed_notifications == [("https://discord.example", "test_close", "msg_999")]

    asyncio.run(scenario())


def test_notify_discord_table_created_stores_message_id_and_handles_fast_close(monkeypatch) -> None:
    closed_notifications: list[tuple[str, str, str]] = []

    async def fake_notify_closed(webhook_url: str, table_key: str, message_id: str) -> None:
        closed_notifications.append((webhook_url, table_key, message_id))

    monkeypatch.setattr(server, "_notify_discord_table_closed", fake_notify_closed)
    monkeypatch.setattr(server, "_post_discord_table_created", lambda *args: "msg_fast_123")

    async def scenario() -> None:
        table = Table("fast_table")
        server.TABLES["fast_table"] = table

        # Normal creation notification stores message id
        await server._notify_discord_table_created("https://discord.example", table, "Alice", Seat.N)
        assert table.discord_message_id == "msg_fast_123"
        assert closed_notifications == []

        # Table is closed before notify completes
        table2 = Table("already_closed")
        table2.is_closed = True
        await server._notify_discord_table_created("https://discord.example", table2, "Bob", Seat.N)
        assert table2.discord_message_id == "msg_fast_123"
        assert closed_notifications == [("https://discord.example", "already_closed", "msg_fast_123")]

    asyncio.run(scenario())


def test_player_label_includes_bot_indicator_and_type() -> None:
    table = Table("labeltest", bot_type="toto")
    table.add_player("Alice", None)
    table.fill_with_bots()
    table.set_bot_type(Seat.E, "cloclo")

    assert server._player_label(table, Seat.N) == "Alice (N/NS)"
    assert server._player_label(table, Seat.E) == f"{table.seats[Seat.E].name} (E/EW) (bot: cloclo)"
    assert server._player_label(table, Seat.S) == f"{table.seats[Seat.S].name} (S/NS) (bot: toto)"
    assert server._player_label(table, Seat.W) == f"{table.seats[Seat.W].name} (W/EW) (bot: toto)"
