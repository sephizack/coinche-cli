"""Entry point for the méta-client.

Run with::

    python -m coinche.meta --host GAME_HOST --port GAME_PORT \\
        --auth-user U --auth-pass P [--listen-host H] [--listen-port N]

Hosts a single authenticated web page that spins up one headless client session
per player, each connecting to the given game server.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from coinche.meta.server import MetaClientServer

DEFAULT_GAME_HOST = "127.0.0.1"
DEFAULT_GAME_PORT = 8765
DEFAULT_LISTEN_PORT = 8080


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coinche méta-client (multi-session web front door)")
    parser.add_argument("--host", default=DEFAULT_GAME_HOST, help=f"Game server host (default: {DEFAULT_GAME_HOST})")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_GAME_PORT, help=f"Game server port (default: {DEFAULT_GAME_PORT})"
    )
    parser.add_argument("--listen-host", default="0.0.0.0", help="Web front-door bind host (default: 0.0.0.0)")
    parser.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help=f"Web front-door port (default: {DEFAULT_LISTEN_PORT})",
    )
    parser.add_argument("--auth-user", default="coinche", help="Basic-auth username (default: coinche)")
    parser.add_argument("--auth-pass", required=True, help="Basic-auth password (required)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log verbosity (default: INFO)",
    )
    return parser


async def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    server = MetaClientServer(
        game_host=args.host,
        game_port=args.port,
        auth_user=args.auth_user,
        auth_pass=args.auth_pass,
        host=args.listen_host,
        port=args.listen_port,
    )
    await server.serve()


def cli() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMéta-client arrêté. À bientôt !")


if __name__ == "__main__":
    cli()
