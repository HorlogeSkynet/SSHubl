import contextlib
import logging
import uuid
from pathlib import Path
from threading import Lock as ThreadingLock

import sublime
import sublime_plugin

from .actions import SshKeepaliveThread
from .project_data import SshSession, remove_from_project_folders, update_window_status
from .ssh_utils import ssh_disconnect, umount_sshfs

_logger = logging.getLogger(__package__)


_ka_threads = {}
_ka_threads_lock = ThreadingLock()


def start_ka_thread_if_needed(window: sublime.Window) -> None:
    """
    This function starts a new `SshKeepaliveThread` for passed `window`, only if there isn't any at
    the moment.
    """
    with _ka_threads_lock:
        if window.id() not in _ka_threads:
            ka_thread = SshKeepaliveThread(window=window)
            ka_thread.start()

            _ka_threads[window.id()] = ka_thread


class EventListener(sublime_plugin.EventListener):
    def on_load_project_async(self, window):
        # sometimes when Sublime re-opens after a hard-crash, `on_new_window_async` hooks may be
        # called before plugin listeners have been set.
        # so we also hook `on_load_project_async` to maximize our chances to start a dedicated
        # keepalive thread for this window
        start_ka_thread_if_needed(window)

    def on_new_window_async(self, window):
        start_ka_thread_if_needed(window)

    def on_pre_close_window(self, window):
        # gracefully stop keepalive thread for this window
        with contextlib.suppress(KeyError):
            _ka_threads.pop(window.id()).stop()

        # when a window is closed, gracefully close all SSH sessions
        # Development note : sessions **are not** removed from project data, so re-connection
        #                    attempts will occur when re-opening the same project
        for identifier, ssh_session in SshSession.get_all_from_project_data(window).items():
            for mount_path in ssh_session.mounts:
                umount_sshfs(Path(mount_path))

            ssh_disconnect(uuid.UUID(identifier))

            ssh_session.is_up = False
            ssh_session.set_in_project_data(window)

            # remove mounts from project folders
            for mount in ssh_session.mounts:
                remove_from_project_folders(mount, window)


class ViewEventListener(sublime_plugin.ViewEventListener):
    def on_load_async(self):
        update_window_status(self.view.window())


def plugin_loaded():
    # make sure there is at least an `SshKeepaliveThread` running _somewhere_
    start_ka_thread_if_needed(sublime.active_window())


def plugin_unloaded():
    # gracefully stop keepalive threads running (maybe) old code (useful for plugin reload)
    while True:
        try:
            _ka_threads.popitem()[1].stop()
        except KeyError:
            break
