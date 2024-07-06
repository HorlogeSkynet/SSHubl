import logging
import sys

import sublime


class ConsoleHandler(logging.StreamHandler):
    """
    `StreamHandler` logging subclass which handles console panel opening when needed.
    """

    def handle(self, record):
        """Open console panel if it is hidden before emitting record"""
        record_name = record.name if record.name != "root" else None
        if (
            logging.getLogger(record_name).isEnabledFor(record.levelno)
            and record.levelno >= logging.WARNING
        ):
            window = sublime.active_window()
            if window.active_panel() != "console":
                window.run_command(
                    "show_panel",
                    {
                        "panel": "console",
                        "toggle": True,
                    },
                )

        if record.levelno >= logging.WARNING:
            self.setStream(sys.stderr)
        else:
            self.setStream(sys.stdout)

        super().handle(record)


def _settings():
    return sublime.load_settings("SSHubl.sublime-settings")


logger = logging.getLogger(__package__)
handler = ConsoleHandler()
handler.setFormatter(logging.Formatter("[%(name)s] %(asctime)s:%(levelname)s: %(message)s"))
logger.addHandler(handler)
logger.propagate = False


def plugin_loaded():
    def _on_change():
        logger.setLevel(logging.DEBUG if _settings().get("debug") else logging.INFO)

    _settings().add_on_change(__name__, _on_change)
    _on_change()


def plugin_unloaded():
    _settings().clear_on_change(__name__)
    logger.removeHandler(handler)
