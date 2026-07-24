"""Pure translation/validation layer for the browser<->client channel (C4).

This is the `WebActionProtocol` from the U2 functional design: a small set of
I/O-free functions that validate inbound browser action frames and encode
outbound state/error frames. It is deliberately distinct from the game wire
protocol (`coinche/protocol.py`) — this module never touches sockets, never
encodes a game-wire message, and never evaluates game legality (BR-U2-1/2).

Frame shapes (see `domain-entities.md`):

Browser -> client::

    {"action": "play"|"bid"|"chat"|"join"|"rematch"|"lobby", ...fields}

Client -> browser::

    {"type": "state", "snapshot": <snapshot_to_dict output>}
    {"type": "error", "code": <str>, "message": <str>}
"""

from __future__ import annotations

import json

# Per-message size cap for inbound browser frames (BR-U2-5). Mirrors the
# server's 64 KiB `readline` DoS guard at this new I/O boundary. A browser
# action frame is always tiny (a card, a bid, a short chat line), so anything
# approaching this bound is malformed or hostile.
MAX_MESSAGE_BYTES = 64 * 1024

# The closed set of browser action verbs the bridge relays to `ClientLink`.
# Anything outside this set is rejected at parse time (BR-U2-5) rather than
# reaching `on_browser_message`.
ALLOWED_ACTIONS: frozenset[str] = frozenset({"play", "bid", "chat", "join", "rematch", "lobby"})

# Required fields per action (BR-U2-5): a frame missing any of these is rejected
# at parse time so `on_browser_message` never hits a KeyError (which would escape
# and hard-close the socket with no error frame). Mirrors how `on_browser_message`
# reads each action; `rematch`/`lobby` carry no payload.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "play": ("card",),
    "bid": ("bid_action",),
    "chat": ("text",),
    "join": ("table_key", "player_name"),
    "rematch": (),
    "lobby": (),
}


class WebProtocolError(Exception):
    """Raised when an inbound browser frame is oversized or malformed.

    The caller (`WebOverlayServer._handle_ws`) catches this, replies with an
    error frame, and keeps the connection open — a single bad frame never
    tears down the browser session (BR-U2-3)."""


def parse_browser_message(raw: str | bytes) -> dict:
    """Validate one inbound browser frame and return its decoded dict.

    Enforces the size cap (BR-U2-5), requires valid JSON decoding to a dict
    with a string ``"action"`` in :data:`ALLOWED_ACTIONS`, and raises
    :class:`WebProtocolError` otherwise. Performs NO game-legality checks and
    does not mutate anything (pure)."""
    # Size cap first — measured in bytes so a multibyte payload can't slip past
    # a character-count check. Reject before attempting to decode a huge blob.
    byte_len = len(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    if byte_len > MAX_MESSAGE_BYTES:
        raise WebProtocolError(f"Message trop volumineux ({byte_len} octets, max {MAX_MESSAGE_BYTES}).")

    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebProtocolError(f"JSON invalide : {exc}") from exc

    if not isinstance(decoded, dict):
        raise WebProtocolError("Le message doit être un objet JSON.")

    action = decoded.get("action")
    if not isinstance(action, str):
        raise WebProtocolError("Champ 'action' manquant ou non textuel.")
    if action not in ALLOWED_ACTIONS:
        raise WebProtocolError(f"Action inconnue : {action!r}.")

    # Reject frames missing an action-specific required field before they reach
    # `on_browser_message` (which would otherwise KeyError and hard-close).
    missing = [field for field in REQUIRED_FIELDS[action] if field not in decoded]
    if missing:
        joined = ", ".join(repr(field) for field in missing)
        raise WebProtocolError(f"Champ(s) requis manquant(s) pour l'action {action!r} : {joined}.")

    return decoded


def encode_state_frame(snapshot: dict) -> str:
    """Encode a full state frame from a `snapshot_to_dict` output (BR-U2-7)."""
    return json.dumps({"type": "state", "snapshot": snapshot})


def encode_error_frame(code: str, message: str) -> str:
    """Encode an error frame the browser can surface to the player."""
    return json.dumps({"type": "error", "code": code, "message": message})
