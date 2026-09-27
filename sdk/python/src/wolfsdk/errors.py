"""Error kinds shared by all bindings (SPEC §1)."""


class WolfSdkError(Exception):
    """Base class of every error raised by the SDK."""


class FormatError(WolfSdkError):
    """Bad magic, truncated data or implausible structure."""


class DeclError(WolfSdkError):
    """A Decl cannot be parsed, or a path is unknown or malformed."""


class NoFitError(WolfSdkError):
    """A replacement does not fit into its archive slot; rebuild instead."""


class ModError(WolfSdkError):
    """A mod.json file is invalid."""
