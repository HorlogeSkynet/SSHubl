import contextlib
import html
import logging
import uuid
from pathlib import Path
from threading import Lock as ThreadingLock
from urllib.parse import urlparse

import sublime
import sublime_plugin

from .actions import SshKeepaliveThread
from .project_data import SshSession, remove_from_project_folders, update_window_status
from .paths import mounts_path
from .ssh_utils import ssh_disconnect, umount_sshfs

# Compatibility shim for Sublime Text < 4132
if int(sublime.version()) >= 4132:
    POPUP_FLAG_KEEP_ON_SELECTION_MODIFIED = sublime.PopupFlags.KEEP_ON_SELECTION_MODIFIED
else:
    POPUP_FLAG_KEEP_ON_SELECTION_MODIFIED = sublime.KEEP_ON_SELECTION_MODIFIED

_logger = logging.getLogger(__package__)


_ka_threads = {}
_ka_threads_lock = ThreadingLock()


def sshfs_remote_file_prelude(view: sublime.View) -> None:
    """
    This function disables GitGutter from view, if it contains a file relative to SSHubl mounts path
    (which is very likely supposed to be a remote file mounted over SSHFS).
    This is to prevent Sublime's Git integration to keep opening file descriptors which mess with
    unmounting operations.
    It requires GitGutter v1.7.5+.

    If the file is relative to SSHubl mount paths but actually resolves outside of a mount point, a
    popup is displayed to warn the user about **local** file edition.
    """
    view_file_name = view.file_name()
    if view_file_name is None:
        # file doesn't exist on disk
        return

    # assert file is relative to SSHubl SSHFS mounts path
    view_file_name_path = Path(view_file_name)
    try:
        view_file_name_path_rel_to_mounts = view_file_name_path.relative_to(mounts_path)
    except ValueError:
        return

    # assert file actually resolves outside of mount point
    view_file_name_path_real = view_file_name_path.resolve()
    try:
        # mount points are known to be 2 level deep relatively to `mounts_path`
        if len(view_file_name_path_real.relative_to(mounts_path).parts) <= 2:
            raise ValueError
    except ValueError:
        pass
    else:
        _logger.debug(
            "%s is likely a remote file, disabling GitGutter from view %d...",
            view_file_name_path,
            view.id(),
        )

        view.settings().update(
            {
                "git_gutter_enable": False,
            }
        )
        return

    # "user path" = path relative to $mounts_path stripped by session and mount UUID prefixes
    view_file_name_user_path = Path(*view_file_name_path_rel_to_mounts.parts[2:])
    # pylint: disable=line-too-long
    popup_content = f"""
        <body id="SSHubl-symlink_warning">
            <style>
                div.warning {{
                    background-color: var(--orangish);
                    color: black;
                    padding: 10px;
                }}
            </style>
            <div class="warning">
                /!\\ SSHubl security warning /!\\<br />
                {html.escape(str(view_file_name_user_path))} actually resolves to a local path on your computer ({html.escape(str(view_file_name_path_real))}) !<br />
                This can happen when SSHFS doesn't remotely follow symbolic links. <a href="sshubl://hide_popup">I understand the risk (HIDE)</a>
            </div>
        </body>
    """
    # pylint: enable=line-too-long

    viewport_width, viewport_height = view.viewport_extent()

    def _on_navigate(href: str) -> None:
        with contextlib.suppress(ValueError):
            parse_result = urlparse(href)
            if parse_result.scheme == "sshubl" and parse_result.hostname == "hide_popup":
                view.hide_popup()

    view.show_popup(
        popup_content,
        flags=POPUP_FLAG_KEEP_ON_SELECTION_MODIFIED,
        max_width=viewport_width,
        max_height=viewport_height,
        on_navigate=_on_navigate,
    )


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
                remove_from_project_folders(mount_path, window)
                umount_sshfs(Path(mount_path))

            ssh_disconnect(uuid.UUID(identifier))

            ssh_session.is_up = False
            ssh_session.set_in_project_data(window)


class ViewEventListener(sublime_plugin.ViewEventListener):
    def on_load_async(self):
        sshfs_remote_file_prelude(self.view)
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
