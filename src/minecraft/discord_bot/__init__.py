# src/minecraft/discord_bot/__init__.py

"""Discord bot peer daemon for the minecraft toolchain (Slice 15).

Architectural position
----------------------
The Discord bot is a **standalone peer daemon** to ``deploy_pack`` and
``modpack_upgrade``, not a submodule of either. It owns Discord
communication end-to-end: inbound interactions, outbound notifications,
approval flow. The CLI and the Textual TUI are sibling I/O adapters over
the same core application logic.

The bot runs on :mod:`DaemonCore`. Its HTTP interaction endpoint
verifies Discord's Ed25519 request signature before any handler runs,
normalizes the payload into an :class:`interactions.InteractionContext`,
dispatches to a handler via :func:`handlers.dispatch`, and replies
through the :class:`client.BotClient` protocol.

Turn breakdown
--------------
- **T2** - transport-neutral foundation: config loader, interaction
  types, client protocol, error hierarchy.
- **T3a** - daemon config loader, Ed25519 signature verification,
  ``minecraft-upgrade`` subprocess seam, slash-command handlers and
  dispatcher.
- **T3b** (this turn) - signed-HTTP interaction endpoint, concrete REST
  client, DaemonCore daemon assembly, ``promote`` approval gate.
- **T3c** - ``POST /notify`` endpoint and :class:`BotDaemonNotifier`
  client that ``deploy_pack/notifications.py`` calls during the
  webhook-to-bot migration.
"""

from __future__ import annotations

from .cli_invoker import CliInvoker, CliResult, FakeCliInvoker, SubprocessCliInvoker
from .client import BotClient, FakeBotClient, SentMessage
from .config import DiscordBotConfig, load_discord_bot_config
from .daemon_config import DiscordDaemonConfig, load_discord_daemon_config
from .discord_client import DiscordRestClient
from .errors import (
    DiscordBotAuthorizationError,
    DiscordBotCliError,
    DiscordBotConfigError,
    DiscordBotDeliveryError,
    DiscordBotError,
    DiscordBotHandlerError,
    DiscordBotSignatureError,
)
from .handlers import CommandHandler, dispatch
from .http_endpoint import (
    APPLICATION_COMMAND_TYPE,
    PING_TYPE,
    DiscordBotPlugin,
    normalize_interaction_context,
)
from .interactions import (
    ChannelRef,
    InteractionContext,
    InteractionRef,
    MessageTarget,
    OutboundMessage,
)
from .main import build_daemon, main
from .signature import verify_discord_signature

__all__ = [
    "APPLICATION_COMMAND_TYPE",
    "PING_TYPE",
    "BotClient",
    "ChannelRef",
    "CliInvoker",
    "CliResult",
    "CommandHandler",
    "DiscordBotAuthorizationError",
    "DiscordBotCliError",
    "DiscordBotConfig",
    "DiscordBotConfigError",
    "DiscordBotDeliveryError",
    "DiscordBotError",
    "DiscordBotHandlerError",
    "DiscordBotPlugin",
    "DiscordBotSignatureError",
    "DiscordDaemonConfig",
    "DiscordRestClient",
    "FakeBotClient",
    "FakeCliInvoker",
    "InteractionContext",
    "InteractionRef",
    "MessageTarget",
    "OutboundMessage",
    "SentMessage",
    "SubprocessCliInvoker",
    "build_daemon",
    "dispatch",
    "load_discord_bot_config",
    "load_discord_daemon_config",
    "main",
    "normalize_interaction_context",
    "verify_discord_signature",
]
