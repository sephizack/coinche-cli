"""Newline-delimited JSON ("JSON Lines") protocol for Coinche client<->server.

Every message is one JSON object `{"type": "<msg_type>", "payload": {...}}`
followed by "\\n", UTF-8 encoded.
"""

from __future__ import annotations

import json

from coinche.rules import ALLOWED_TRUMPS

# --- Client -> Server message types -------------------------------------------

JOIN = "join"
BID = "bid"
PLAY_CARD = "play_card"
CHAT = "chat"

CLIENT_MESSAGE_TYPES = {JOIN, BID, PLAY_CARD, CHAT}

# --- Server -> Client message types -------------------------------------------

JOINED = "joined"
LOBBY_UPDATE = "lobby_update"
DEAL = "deal"
BID_REQUEST = "bid_request"
BID_UPDATE = "bid_update"
BIDDING_RESULT = "bidding_result"
PLAY_REQUEST = "play_request"
CARD_PLAYED = "card_played"
TRICK_RESULT = "trick_result"
ROUND_SCORE = "round_score"
GAME_OVER = "game_over"
RESYNC = "resync"
CONNECTION_STATUS = "connection_status"
ERROR = "error"

SERVER_MESSAGE_TYPES = {
    JOINED,
    LOBBY_UPDATE,
    DEAL,
    BID_REQUEST,
    BID_UPDATE,
    BIDDING_RESULT,
    PLAY_REQUEST,
    CARD_PLAYED,
    TRICK_RESULT,
    ROUND_SCORE,
    GAME_OVER,
    RESYNC,
    CONNECTION_STATUS,
    ERROR,
    CHAT,  # chat is also broadcast server -> client
}

ALL_MESSAGE_TYPES = CLIENT_MESSAGE_TYPES | SERVER_MESSAGE_TYPES

# Error codes used in `error` messages.
NOT_YOUR_TURN = "NOT_YOUR_TURN"
ILLEGAL_BID = "ILLEGAL_BID"
ILLEGAL_CARD = "ILLEGAL_CARD"
TABLE_FULL = "TABLE_FULL"
GAME_IN_PROGRESS = "GAME_IN_PROGRESS"
NAME_TAKEN = "NAME_TAKEN"
MALFORMED_MESSAGE = "MALFORMED_MESSAGE"

# Required payload fields for client -> server messages only (defense against
# malformed/untrusted client input; server -> client messages are trusted).
REQUIRED_FIELDS: dict[str, set[str]] = {
    JOIN: {"table_key", "player_name"},
    BID: {"action"},
    PLAY_CARD: {"card"},
    CHAT: {"text"},
}
# JOIN also accepts an optional "preferred_partner" field (a player name to try to
# be seated on the same team as, best-effort; see Table.add_player).

_VALID_BID_ACTIONS = {"pass", "bid", "coinche", "surcoinche"}


class ProtocolError(Exception):
    """Raised when a message is malformed, oversized, or fails validation."""


def encode(msg_type: str, payload: dict) -> bytes:
    """Encode a message as one line of newline-terminated UTF-8 JSON."""
    return (json.dumps({"type": msg_type, "payload": payload}) + "\n").encode("utf-8")


def decode(line: bytes) -> tuple[str, dict]:
    """Decode one line into (msg_type, payload). Raises ProtocolError on failure."""
    try:
        text = line.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProtocolError("Invalid UTF-8 in message") from exc

    if not text:
        raise ProtocolError("Empty message")

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("Malformed JSON") from exc

    if not isinstance(obj, dict):
        raise ProtocolError("Message must be a JSON object")

    msg_type = obj.get("type")
    payload = obj.get("payload")

    if not isinstance(msg_type, str) or msg_type not in ALL_MESSAGE_TYPES:
        raise ProtocolError(f"Unknown or missing message type: {msg_type!r}")
    if not isinstance(payload, dict):
        raise ProtocolError("Message payload must be a JSON object")

    if msg_type in CLIENT_MESSAGE_TYPES:
        _validate_client_payload(msg_type, payload)

    return msg_type, payload


def _validate_client_payload(msg_type: str, payload: dict) -> None:
    required = REQUIRED_FIELDS.get(msg_type, set())
    missing = required - payload.keys()
    if missing:
        raise ProtocolError(f"Missing required fields for {msg_type!r}: {sorted(missing)}")

    if msg_type == BID:
        action = payload.get("action")
        if action not in _VALID_BID_ACTIONS:
            raise ProtocolError(f"Unknown bid action: {action!r}")
        if action == "bid":
            trump = payload.get("trump")
            if trump not in ALLOWED_TRUMPS:
                raise ProtocolError(f"Unknown or illegal trump declaration: {trump!r}")
            points = payload.get("points")
            if points != "capot" and not isinstance(points, int):
                raise ProtocolError(f"Invalid points value: {points!r}")
