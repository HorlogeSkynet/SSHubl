import logging
import time
import typing
import uuid
from pathlib import Path, PurePath
from threading import Thread

import sublime

from .project_data import (
    SshSession,
    add_to_project_folders,
    remove_from_project_folders,
    update_window_status,
)
from .project_data import lock as project_data_lock
from .ssh_utils import (
    PasswordlessConnectionException,
    mount_sshfs,
    ssh_check_master,
    ssh_connect,
    ssh_connect_interactive,
    ssh_disconnect,
    ssh_forward,
    umount_sshfs,
)
from .st_utils import (
    format_ip_addr,
    get_absolute_purepath_flavour,
    parse_ssh_connection,
)

_logger = logging.getLogger(__package__)


def _on_connection(
    view: sublime.View,
    ssh_session: SshSession,
    mounts: typing.Optional[typing.Dict[str, str]] = None,
    forwards: typing.Optional[typing.List[dict]] = None,
) -> None:
    # store SSH session metadata in project data
    # Development note : on **re-connection**, mounts and forwards are reset here and will be
    #                    directly re-populated by thread actions below
    ssh_session.set_in_project_data(view.window())

    _logger.info("successfully connected to %s !", ssh_session)

    update_window_status(view.window())

    # re-mount and re-open previous remote folders (if any)
    for mount_path, remote_path in (mounts or {}).items():
        SshMountSshfs(
            view,
            uuid.UUID(ssh_session.identifier),
            # here paths are strings due to JSON serialization, infer flavour back for remote
            mount_path=Path(mount_path),
            remote_path=typing.cast(PurePath, get_absolute_purepath_flavour(remote_path)),
        ).start()

    # re-open previous forwards (if any)
    for forward in forwards or []:
        # infer original forwarding rule from "local" and "remote" targets
        is_reverse = forward["is_reverse"]
        target_1, target_2 = (
            forward["target_remote"] if is_reverse else forward["target_local"],
            forward["target_local"] if is_reverse else forward["target_remote"],
        )

        SshForward(
            view,
            uuid.UUID(ssh_session.identifier),
            is_reverse,
            target_1,
            target_2,
        ).start()


class SshConnect(Thread):
    def __init__(  # pylint: disable=too-many-arguments
        self,
        view: sublime.View,
        connection_str: str,
        identifier: typing.Optional[uuid.UUID] = None,
        mounts: typing.Optional[typing.Dict[str, str]] = None,
        forwards: typing.Optional[typing.List[dict]] = None,
    ):
        self.view = view
        self.connection_str = connection_str

        # below attributes are only used in case of re-connection
        self.identifier = identifier
        self.mounts = mounts or {}
        self.forwards = forwards or []

        super().__init__()

    def run(self):
        host, port, login, password = parse_ssh_connection(self.connection_str)

        _logger.debug(
            "SSH connection string is : %s:%s@%s:%d",
            login,
            "*" * len(password or ""),
            format_ip_addr(host),
            port,
        )

        self.view.set_status(
            "zz_connection_in_progress",
            f"Connecting to ssh://{login}@{format_ip_addr(host)}:{port}...",
        )
        try:
            try:
                identifier = ssh_connect(host, port, login, password, self.identifier)
            except PasswordlessConnectionException:
                _logger.info(
                    "authentication failed for %s@%s:%d, prompting for password before retrying...",
                    login,
                    format_ip_addr(host),
                    port,
                )

                # we simply leave here and let `ssh_connect_password` command call this action again
                schedule_ssh_connect_password_command(
                    host,
                    port,
                    login,
                    self.identifier,
                    self.mounts,
                    self.forwards,
                    self.view.window(),
                )
                return
        finally:
            self.view.erase_status("zz_connection_in_progress")

        if identifier is not None:
            _on_connection(
                self.view,
                SshSession(str(identifier), host, port, login),
                self.mounts,
                self.forwards,
            )


class SshInteractiveConnectionWatcher(Thread):
    def __init__(  # pylint: disable=too-many-arguments
        self,
        view: sublime.View,
        identifier: uuid.UUID,
        connection_str: str,
        mounts: typing.Optional[typing.Dict[str, str]] = None,
        forwards: typing.Optional[typing.List[dict]] = None,
    ):
        self.view = view
        self.identifier = identifier
        self.connection_str = connection_str

        # below attributes are only used in case of re-connection
        self.mounts = mounts or {}
        self.forwards = forwards or []

        super().__init__()

    def run(self):
        _logger.debug(
            "interactive connection watcher is starting up for %s (view=%d)...",
            self.identifier,
            self.view.id(),
        )

        host, port, login, _ = parse_ssh_connection(self.connection_str)

        _logger.debug(
            "SSH connection string is : %s@%s:%d",
            login,
            format_ip_addr(host),
            port,
        )

        self.view.set_status(
            "zz_connection_in_progress",
            f"Connecting to ssh://{login}@{format_ip_addr(host)}:{port}...",
        )
        try:
            while True:
                # we fetch view "validity" _here_ to prevent a race condition when user closes the
                # view right *after* we actually checked whether connection succeeded.
                is_view_valid = self.view.is_valid()

                # when master is considered "up" (i.e. client successfully connected to server), run
                # connection postlude and leave
                if ssh_check_master(self.identifier):
                    _on_connection(
                        self.view,
                        SshSession(str(self.identifier), host, port, login, is_interactive=True),
                        self.mounts,
                        self.forwards,
                    )
                    break

                # stop this thread if view was closed (i.e. client has terminated)
                if not is_view_valid:
                    # if view corresponded to a reconnection attempt, we have to update `is_up`
                    # session attribute as current attempt failed
                    with project_data_lock:
                        ssh_session = SshSession.get_from_project_data(self.identifier)
                        if ssh_session is not None:
                            ssh_session.is_up = False
                            ssh_session.set_in_project_data(self.view.window())
                    break

                time.sleep(2)
        finally:
            self.view.erase_status("zz_connection_in_progress")

        _logger.debug(
            "interactive connection watcher is shutting down for %s (view=%d)...",
            self.identifier,
            self.view.id(),
        )


class SshDisconnect(Thread):
    def __init__(self, view: sublime.View, identifier: uuid.UUID):
        self.view = view
        self.identifier = identifier

        super().__init__()

    def run(self):
        ssh_session = SshSession.get_from_project_data(self.identifier, self.view.window())
        if ssh_session is not None:
            # properly unmount sshfs before disconnecting session
            for mount_path in ssh_session.mounts:
                unmount_thread = SshMountSshfs(
                    self.view, self.identifier, do_mount=False, mount_path=Path(mount_path)
                )
                unmount_thread.start()
                unmount_thread.join()

        self.view.set_status("zz_disconnection_in_progress", f"Disconnecting from {ssh_session}...")
        try:
            ssh_disconnect(self.identifier)
        finally:
            self.view.erase_status("zz_disconnection_in_progress")

        if ssh_session is not None:
            ssh_session.remove_from_project_data(self.view.window())

        update_window_status(self.view.window())


class SshForward(Thread):
    def __init__(  # pylint: disable=too-many-arguments
        self,
        view: sublime.View,
        identifier: uuid.UUID,
        is_reverse: bool,
        fwd_target_1: str,
        fwd_target_2: str,
        *,
        do_open: bool = True,
    ):
        self.view = view
        self.identifier = identifier
        self.is_reverse = is_reverse
        self.fwd_target_1 = fwd_target_1
        self.fwd_target_2 = fwd_target_2
        self.do_open = do_open

        super().__init__()

    def run(self):
        self.view.set_status(
            "zz_forward_in_progress",
            f"{'Request' if self.do_open else 'Cancel'} forwarding {self.fwd_target_1} "
            f"{'<-' if self.is_reverse else '->'} {self.fwd_target_2}...",
        )
        try:
            forward_rule = ssh_forward(
                self.identifier, self.do_open, self.is_reverse, self.fwd_target_1, self.fwd_target_2
            )
        finally:
            self.view.erase_status("zz_forward_in_progress")

        if forward_rule is None:
            return

        # store forwarding rule in SSH session metadata
        with project_data_lock:
            ssh_session = SshSession.get_from_project_data(self.identifier, self.view.window())
            if ssh_session is None:
                return

            if self.do_open:
                if forward_rule not in ssh_session.forwards:
                    ssh_session.forwards.append(forward_rule)
            else:
                # clean SSH session forwards by removing the one that has just been closed
                ssh_session.forwards = [
                    forward
                    for forward in ssh_session.forwards
                    if not ssh_session.is_same_forward(forward, forward_rule)
                ]

            ssh_session.set_in_project_data(self.view.window())

        update_window_status(self.view.window())


class SshMountSshfs(Thread):
    def __init__(  # pylint: disable=too-many-arguments
        self,
        view: sublime.View,
        identifier: uuid.UUID,
        *,
        do_mount: bool = True,
        mount_path: typing.Optional[Path] = None,
        remote_path: typing.Optional[PurePath] = None,
    ):
        self.view = view
        self.identifier = identifier
        self.do_mount = do_mount
        self.mount_path = mount_path
        self.remote_path = remote_path

        super().__init__()

    def run(self):
        ssh_session = SshSession.get_from_project_data(self.identifier, self.view.window())
        if ssh_session is None:
            _logger.error("could not retrieve SSH session information from project data")
            return

        # if `remote_path` is unknown, fetch if from session
        if not self.do_mount:
            self.remote_path = PurePath(ssh_session.mounts[str(self.mount_path)])

        self.view.set_status(
            "zz_mounting_sshfs",
            f"{'M' if self.do_mount else 'Unm'}ounting ssh://{ssh_session}{self.remote_path}...",
        )
        try:
            # Do-mounting : mount -> add folder to project
            # Do-unmounting : remove folder from project -> unmount
            if self.do_mount:
                mount_path = mount_sshfs(
                    self.identifier, typing.cast(PurePath, self.remote_path), self.mount_path
                )
                if mount_path is None:
                    return
                add_to_project_folders(
                    str(mount_path), f"{ssh_session}{self.remote_path}", self.view.window()
                )
            else:
                mount_path = typing.cast(Path, self.mount_path)
                remove_from_project_folders(str(mount_path), self.view.window())
                umount_sshfs(mount_path)
        finally:
            self.view.erase_status("zz_mounting_sshfs")

        # store/remove mount path in/from SSH session metadata
        with project_data_lock:
            if self.do_mount:
                ssh_session.mounts[str(mount_path)] = str(self.remote_path)
            else:
                ssh_session.mounts.pop(str(mount_path), None)

            ssh_session.set_in_project_data(self.view.window())


class SshKeepaliveThread(Thread):
    """
    This thread is responsible for periodical connections to OpenSSH control master sockets, in
    order to postpone `ControlPersist` timeout and thus keep these sessions opened.
    If master fails to answer, a re-connection attempt occurs.
    """

    __LOOP_PERIOD = 10

    def __init__(self, *args, window: sublime.Window, **kwargs):
        self.window = window

        self._keep_running = True

        super().__init__(*args, **kwargs)

    def stop(self) -> None:
        self._keep_running = False

    def run(self):
        _logger.debug(
            "keepalive thread %d for window %d is starting up...", self.ident, self.window.id()
        )

        while self._keep_running:
            start_loop = time.monotonic()

            for identifier in SshSession.get_all_from_project_data(self.window):
                session_identifier = uuid.UUID(identifier)
                with project_data_lock:
                    ssh_session = SshSession.get_from_project_data(session_identifier, self.window)

                    # skip this session as a re-connection attempt is already in progress
                    if ssh_session is None or ssh_session.is_up is None:
                        continue

                    _logger.debug(
                        "checking that master behind %s (%s) is still up...",
                        ssh_session,
                        identifier,
                    )
                    is_up = ssh_check_master(session_identifier)
                    if is_up:  # update session "up" status (if needed) and leave
                        if not ssh_session.is_up:
                            ssh_session.is_up = is_up
                            ssh_session.set_in_project_data(self.window)
                        continue

                    _logger.warning("%s's master is down : attempting to reconnect...", ssh_session)
                    if ssh_session.is_interactive:
                        ssh_connect_interactive(
                            str(ssh_session),
                            session_identifier,
                            ssh_session.mounts,
                            ssh_session.forwards,
                            self.window,
                        )
                    else:
                        SshConnect(
                            self.window.active_view(),
                            str(ssh_session),
                            session_identifier,
                            ssh_session.mounts,
                            ssh_session.forwards,
                        ).start()

                    # set "up" status to `None` so we know a re-connection attempt is in progress
                    ssh_session.is_up = None
                    ssh_session.set_in_project_data(self.window)

            end_loop = time.monotonic()

            # sleep at most `__LOOP_PERIOD` seconds
            time.sleep(
                max(min(self.__LOOP_PERIOD - (end_loop - start_loop), self.__LOOP_PERIOD), 0)
            )

        _logger.debug(
            "keepalive thread %d for window %d is shutting down...", self.ident, self.window.id()
        )


def schedule_ssh_connect_password_command(  # pylint: disable=too-many-arguments
    host: str,
    port: int,
    login: str,
    identifier: typing.Optional[uuid.UUID] = None,
    mounts: typing.Optional[typing.Dict[str, str]] = None,
    forwards: typing.Optional[typing.List[dict]] = None,
    window: typing.Optional[sublime.Window] = None,
    *,
    delay: int = 0,
) -> None:
    if window is None:
        window = sublime.active_window()

    if delay != 0:
        _logger.debug(
            "scheduling password connection command for %s to be run on window %d in %d seconds...",
            f"{login}@{format_ip_addr(host)}:{port}",
            window.id(),
            delay,
        )

    sublime.set_timeout_async(
        lambda: window.run_command(
            "ssh_connect_password",
            {
                "host": host,
                "port": port,
                "login": login,
                "identifier": str(identifier) if identifier is not None else None,
                "mounts": mounts,
                "forwards": forwards,
            },
        ),
        delay,
    )
