import importlib
import sys

import sublime

ST_REQUIRED_MINIMUM_VERSION = 4081

for suffix in (
    "paths",
    "project_data",
    "ssh_utils",
    "st_utils",
    "actions",
    # reload commands and listeners at last as they depend on other modules
    "commands",
    "listeners",
):
    module = f"{__package__}.sshubl.{suffix}"
    if module in sys.modules:
        importlib.reload(sys.modules[module])

if int(sublime.version()) < ST_REQUIRED_MINIMUM_VERSION:
    sublime.error_message(f"Sublime Text {ST_REQUIRED_MINIMUM_VERSION}+ is required !")
else:
    # fmt: off
    from .sshubl.commands import (  # type: ignore[import-untyped]  # pylint: disable=unused-import
        SshCancelForwardCommand,
        SshCloseDirCommand,
        SshConnectCommand,
        SshConnectPasswordCommand,
        SshDisconnectCommand,
        SshOpenDirCommand,
        SshRequestForwardCommand,
        SshSelectDirCommand,
        SshTerminalCommand,
    )
    from .sshubl.listeners import (  # type: ignore[import-untyped]  # pylint: disable=unused-import
        EventListener,
        ViewEventListener,
        plugin_loaded,
        plugin_unloaded,
    )
    # fmt: on
