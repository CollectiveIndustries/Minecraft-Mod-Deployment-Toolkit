# src/minecraft/discord_bot/errors.py

"""Error hierarchy for the Discord bot daemon (Slice 15).

Peer packages carry peer error hierarchies. The bot is a standalone
daemon at the same level as :mod:`minecraft.deploy_pack` and
:mod:`minecraft.modpack_upgrade`; its exceptions do not inherit from
:class:`minecraft.modpack_upgrade.errors.UpgradeError`, and the reverse
is also true. The two processes communicate via the ``minecraft-upgrade``
CLI subprocess, not via an in-process exception boundary, so sharing a
base class would couple them without any benefit.

Subclasses
----------
- :class:`DiscordBotConfigError` - invalid or missing configuration
  when the bot is meant to be enabled.
- :class:`DiscordBotSignatureError` - the configured Ed25519 public
  key is malformed. Request-tampering evidence is *not* an exception;
  :func:`minecraft.discord_bot.signature.verify_discord_signature`
  returns ``False`` for a bad signature and raises this only for a
  misconfigured public key.
- :class:`DiscordBotAuthorizationError` - a user or role is not in the
  allowlist. Raised by the dispatcher before any handler runs.
- :class:`DiscordBotHandlerError` - a handler detected a condition the
  dispatcher should surface to the user as a command failure.
- :class:`DiscordBotCliError` - the ``minecraft-upgrade`` subprocess
  failed to start, timed out, or could not be located.
- :class:`DiscordBotDeliveryError` - Discord REST delivery failed.
  Raised by :class:`minecraft.discord_bot.discord_client.DiscordRestClient`
  on network errors or non-2xx responses. The transport never returns
  silently after a failed delivery.
"""

from __future__ import annotations

__all__ = [
    "DiscordBotAuthorizationError",
    "DiscordBotCliError",
    "DiscordBotConfigError",
    "DiscordBotDeliveryError",
    "DiscordBotError",
    "DiscordBotHandlerError",
    "DiscordBotSignatureError",
]


class DiscordBotError(Exception):
    """Base exception for every Discord bot error.

    Every error raised by this package should be an instance of this
    class (or a subclass). Callers that want to distinguish bot errors
    from unrelated exceptions catch :class:`DiscordBotError`.
    """


class DiscordBotConfigError(DiscordBotError):
    """The bot configuration is invalid or missing when it should be present.

    Raised when ``config.d/upgrade.toml`` or ``config.d/discord_bot.toml``
    is malformed, or when a required field is absent while an
    affirmative "enable the bot" signal (a section plus a token) is
    present.

    A **disabled** bot is not a configuration error: when the
    ``[discord]`` section is absent, or when the token is absent, the
    application-config loader returns ``None`` and this exception is
    not raised.
    """


class DiscordBotSignatureError(DiscordBotError):
    """The configured Ed25519 public key is malformed.

    Raised by :func:`minecraft.discord_bot.signature.verify_discord_signature`
    when ``public_key_hex`` is not 64 lowercase hex characters or does
    not decode to a 32-byte value. That is an operator-setup bug, not
    evidence of a tampered request. A bad request signature returns
    ``False`` from the verifier; only misconfiguration raises this.
    """


class DiscordBotAuthorizationError(DiscordBotError):
    """A user is not authorized to invoke a command.

    Raised by the dispatcher when neither the user ID nor any of the
    user's role IDs appears in the configured allowlists. Empty
    allowlists deny everyone: operators must explicitly authorize at
    least one user or role before the bot will respond.
    """


class DiscordBotHandlerError(DiscordBotError):
    """A handler detected a command failure the dispatcher should surface.

    Raised by a handler when it wants the dispatcher to reply with a
    user-facing "command failed" message. Handlers may also reply
    directly for usage errors; either pattern is acceptable. Letting
    unexpected exceptions propagate is also fine: the dispatcher
    converts them to a generic "internal error" reply.
    """


class DiscordBotCliError(DiscordBotError):
    """The ``minecraft-upgrade`` CLI subprocess failed to run.

    Raised by :class:`minecraft.discord_bot.cli_invoker.SubprocessCliInvoker`
    when the binary is missing, cannot be started, or exceeds the
    configured timeout. A non-zero exit code from a successfully-started
    subprocess is *not* an exception: it is reported in the returned
    :class:`CliResult`.
    """


class DiscordBotDeliveryError(DiscordBotError):
    """Discord REST delivery failed.

    Raised by :class:`minecraft.discord_bot.discord_client.DiscordRestClient`
    when a network error occurs, when Discord returns a non-2xx
    response, or when the request itself is malformed. A silent return
    after failed delivery violates the "deliver exactly once or raise"
    contract; callers distinguish delivery failure from "bot disabled"
    by catching this class specifically.
    """
