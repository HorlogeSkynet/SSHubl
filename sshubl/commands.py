import functools
import shlex
import typing
import uuid
from abc import ABC
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from threading import Lock as ThreadingLock

import sublime
import sublime_plugin

from .actions import (
    SshConnect,
    SshDisconnect,
    SshForward,
    SshMountSshfs,
    schedule_ssh_connect_password_command,
)
from .project_data import SshSession
from .ssh_utils import (
    get_base_ssh_cmd,
    ssh_exec,
)
from .st_utils import (
    format_ip_addr,
    get_absolute_purepath_flavour,
    is_package_installed,
    parse_ssh_connection,
    validate_forward_target,
)

settings = sublime.load_settings("SSHubl.sublime-settings")

# this lock is used to prevent multiple `SshConnectPassword` window commands to run simultaneously
# Development note : this lock **must not** be blocking not re-entrant as commands are run by an
#                    unique (separate) thread that would be globally blocked
ssh_connect_password_command_lock = ThreadingLock()


def _with_session_identifier(func):
    """
    Function decorator calling `func` by setting `identifier` as first keyword argument, even when
    it hasn't been set (by defaulting to _first_ SSH session available).
    It allows transparent usage of `TextCommand` depending on `_SshSessionInputHandler` as first
    input (which may not return any session identifier when there isn't multiple to choose from).
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        identifier = kwargs.pop("identifier", None)
        if identifier is None:
            # fetch `Window` reference from `View` (if possible)
            try:
                window = args[0].view.window()
            except (IndexError, AttributeError):
                window = None

            identifier = SshSession.get_identifiers_from_project_data(window)[0]

        return func(*args, identifier=identifier, **kwargs)

    return wrapper


class _WithWindowAndSessionCommandInputHandler(ABC, sublime_plugin.CommandInputHandler):
    """
    Abstract command input handler which requires a window and an SSH session identifier as keyword
    arguments.
    This is useful for commands interacting with "project data" and/or a specific SSH session.
    """

    def __init__(self, *args, window: sublime.Window, identifier: uuid.UUID, **kwargs):
        self.window = window
        self.identifier = identifier

        super().__init__(*args, **kwargs)


class _SshSessionInputHandler(sublime_plugin.ListInputHandler):
    """
    Input handler allowing to select an SSH session from the list of current active ones, before
    handing off to the next specified input handler (`next_input_handler`).
    Switch `with_mounts` to `True` to only list SSH sessions with mounted remote directories.
    Switch `with_forwards` to `True` to only list SSH sessions with opened forwards.

    Note : this handler is **skipped** when there is less than two sessions to choose from. Please
           see `_with_session_identifier` decorator to populate `identifier` parameter with a
           default value (the only existing session ?).
    """

    def __new__(
        cls,
        *_,
        window: typing.Optional[sublime.Window] = None,
        next_input_handler: typing.Optional[
            typing.Type[_WithWindowAndSessionCommandInputHandler]
        ] = None,
        **__,
    ):
        ssh_sessions = SshSession.get_identifiers_from_project_data(window)

        # only process this input handler when there are multiple sessions
        if len(ssh_sessions) > 1:
            return super().__new__(cls)  # pylint: disable=no-value-for-parameter

        # directly switch to next input handler (if any)
        if next_input_handler is not None:
            return next_input_handler(identifier=uuid.UUID(ssh_sessions[0]), window=window)

        # skip this input handler completely otherwise
        return None

    def __init__(
        self,
        *args,
        window: typing.Optional[sublime.Window] = None,
        next_input_handler: typing.Optional[
            typing.Type[_WithWindowAndSessionCommandInputHandler]
        ] = None,
        with_mounts: bool = False,
        with_forwards: bool = False,
        **kwargs,
    ):
        self.window = window
        self.next_input_handler = next_input_handler
        self.with_mounts = with_mounts
        self.with_forwards = with_forwards

        super().__init__(*args, **kwargs)

    def name(self):
        return "identifier"

    def description(self, _value, _text):
        return ""

    def list_items(self):
        return [
            (str(ssh_session), identifier)
            for identifier, ssh_session in SshSession.get_all_from_project_data(self.window).items()
            # filter sessions which have opened mounts/forwards (if flags are set)
            if (
                (not self.with_forwards or ssh_session.forwards)
                and (not self.with_mounts or ssh_session.mounts)
            )
        ]

    def next_input(self, args):
        if self.next_input_handler is None:
            return None

        return self.next_input_handler(window=self.window, identifier=uuid.UUID(args[self.name()]))


# --- BEGIN (DIS)CONNECT COMMANDS ---


class _ConnectInputHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "connection_str"

    def placeholder(self):
        return "user[:password]@host[:port]"

    def validate(self, text):
        try:
            host, *_ = parse_ssh_connection(text)
        except ValueError:
            return False

        return bool(host)


class SshConnectCommand(sublime_plugin.TextCommand):
    def run(self, _edit, connection_str: str):
        SshConnect(self.view, connection_str).start()

    def input(self, _args):
        return _ConnectInputHandler()

    def input_description(self):
        return "SSH: Connect to server"


class SshConnectPasswordCommand(sublime_plugin.WindowCommand):
    """
    (Hidden) command manually run by `SshConnect` action, which asks for a password before
    (re)trying connection to specified host.
    Current active panel (in Sublime's terms) is saved and then re-opened afterwards.
    """

    def run(  # pylint: disable=too-many-arguments
        self,
        host: str,
        port: int,
        login: str,
        identifier: typing.Optional[str] = None,
        mounts: typing.Optional[typing.Dict[str, str]] = None,
        forwards: typing.Optional[typing.List[dict]] = None,
    ):
        previous_active_panel = self.window.active_panel()

        # run this command again in 5 seconds when an input panel is already open, or when another
        # `ssh_connect_password_command` is currently running
        # Development note : this is required to prevent password input panels to interrupt current
        #                    user input flow.
        if previous_active_panel == "input" or not ssh_connect_password_command_lock.acquire(  # pylint: disable=consider-using-with
            blocking=False
        ):
            schedule_ssh_connect_password_command(
                host, port, login, uuid.UUID(identifier), mounts, forwards, self.window, delay=5000
            )
            return

        panel = self.window.show_input_panel(
            caption=f"{login}@{format_ip_addr(host)}:{port}'s password:",
            initial_text="",
            on_done=functools.partial(
                self._on_done,
                host=host,
                port=port,
                login=login,
                identifier=identifier,
                mounts=mounts,
                forwards=forwards,
                panel_to_open=previous_active_panel,
            ),
            on_change=None,
            on_cancel=functools.partial(
                self._on_cancel,
                host=host,
                port=port,
                login=login,
                identifier=identifier,
                mounts=mounts,
                forwards=forwards,
                panel_to_open=previous_active_panel,
            ),
        )
        panel.settings().set("password", True)

    def is_visible(self):
        return False

    def _on_done(  # pylint: disable=too-many-arguments
        self,
        password: str,
        *,
        host: str,
        port: int,
        login: str,
        identifier: typing.Optional[str] = None,
        mounts: typing.Optional[typing.Dict[str, str]] = None,
        forwards: typing.Optional[typing.List[dict]] = None,
        panel_to_open: typing.Optional[str] = None,
    ) -> None:
        # make sure `_finish` method is called
        try:
            # call `SshConnect` action again, with input password
            SshConnect(
                self.window.active_view(),
                f"{login}:{password}@{format_ip_addr(host)}:{port}",
                uuid.UUID(identifier) if identifier is not None else None,
                mounts,
                forwards,
            ).start()
        finally:
            self._finish(panel_to_open)

    def _on_cancel(  # pylint: disable=too-many-arguments
        self,
        host: str,
        port: int,
        login: str,
        identifier: typing.Optional[str] = None,
        mounts: typing.Optional[typing.Dict[str, str]] = None,
        forwards: typing.Optional[typing.List[dict]] = None,
        *,
        panel_to_open: typing.Optional[str] = None,
    ) -> None:
        # make sure `_finish` method is called
        try:
            # if this connection corresponds to a known session re-schedule an attempt in 10 seconds
            if identifier is not None:
                schedule_ssh_connect_password_command(
                    host,
                    port,
                    login,
                    uuid.UUID(identifier),
                    mounts,
                    forwards,
                    self.window,
                    delay=10000,
                )
        finally:
            self._finish(panel_to_open)

    def _finish(self, panel_to_open: typing.Optional[str] = None):
        # make sure lock is released
        try:
            if panel_to_open is not None:
                self.window.run_command(
                    "show_panel",
                    {
                        "panel": panel_to_open,
                        "toggle": True,
                    },
                )
        finally:
            ssh_connect_password_command_lock.release()


class SshDisconnectCommand(sublime_plugin.TextCommand):
    @_with_session_identifier
    def run(self, _edit, identifier: str):
        SshDisconnect(self.view, uuid.UUID(identifier)).start()

    def is_enabled(self):
        return bool(SshSession.get_identifiers_from_project_data(self.view.window()))

    def input(self, _args):
        return _SshSessionInputHandler(window=self.view.window())

    def input_description(self):
        return "SSH: Disconnect session"


# --- END (DIS)CONNECT COMMANDS ---


# --- BEGIN FORWARDING COMMANDS ---


class _ForwardTargetInputHandler(ABC, sublime_plugin.TextInputHandler):
    _INITIAL_TEXT = "127.0.0.1:"
    _INITIAL_TEXT_LENGTH = len(_INITIAL_TEXT)

    _TIPS_FORMAT_STRING = "Tips : {side} socket (TCP address or UNIX path) to {action}"

    def __init__(self, *args, is_reverse: bool, **kwargs):
        self.is_reverse = is_reverse

        super().__init__(*args, **kwargs)

    def initial_text(self):
        return self._INITIAL_TEXT

    def initial_selection(self):
        """Disable initial selection and set the cursor next to colon"""
        return [(self._INITIAL_TEXT_LENGTH, self._INITIAL_TEXT_LENGTH)]

    def validate(self, text):
        return validate_forward_target(text)


class _ForwardTarget2InputHandler(_ForwardTargetInputHandler):
    def name(self):
        return "fwd_target_2"

    def preview(self, _text):
        """
        We divert `preview` panel to give some tips about expected input.
        Going through `placeholder` is messy and prevent us from preparing loopback address.
        """
        # in case of reverse forward, the second target corresponds to a local socket
        return self._TIPS_FORMAT_STRING.format(
            side="local" if self.is_reverse else "remote",
            action="forward to",
        )


class _ForwardTarget1InputHandler(_ForwardTargetInputHandler):
    def name(self):
        return "fwd_target_1"

    def preview(self, _text):
        """
        We leverage `preview` panel to give some tips about expected input.
        Going through `placeholder` is messy and prevent us from preparing loopback address.
        """
        # in case of reverse forward, the first target corresponds to a remote socket
        return self._TIPS_FORMAT_STRING.format(
            side="remote" if self.is_reverse else "local",
            action="listen on",
        )

    def next_input(self, _args):
        return _ForwardTarget2InputHandler(is_reverse=self.is_reverse)


class _ReverseForwardInputHandler(
    sublime_plugin.ListInputHandler, _WithWindowAndSessionCommandInputHandler
):
    def name(self):
        return "is_reverse"

    def description(self, is_reverse, _text):
        return "-R" if is_reverse else "-L"

    def list_items(self):
        return [
            ("Forward (-L)", False),
            ("Reverse (-R)", True),
        ]

    def next_input(self, args):
        return _ForwardTarget1InputHandler(is_reverse=args[self.name()])


class SshRequestForwardCommand(sublime_plugin.TextCommand):
    @_with_session_identifier
    def run(
        self, _edit, identifier: str, is_reverse: bool, fwd_target_1: str, fwd_target_2: str
    ) -> None:
        SshForward(
            self.view,
            uuid.UUID(identifier),
            is_reverse,
            fwd_target_1,
            fwd_target_2,
        ).start()

    def is_enabled(self):
        return bool(SshSession.get_identifiers_from_project_data(self.view.window()))

    def input(self, _args):
        return _SshSessionInputHandler(
            window=self.view.window(), next_input_handler=_ReverseForwardInputHandler
        )

    def input_description(self):
        return "SSH: Open forward"


class _SshForwardInputHandler(
    sublime_plugin.ListInputHandler, _WithWindowAndSessionCommandInputHandler
):
    def name(self):
        return "forward"

    def list_items(self):
        return [
            (
                f"{forward['target_local']} "
                f"{'<-' if forward['is_reverse'] else '->'} "
                f"{forward['target_remote']}",
                forward,
            )
            for forward in SshSession.get_all_from_project_data(self.window)[
                str(self.identifier)
            ].forwards
        ]


class SshCancelForwardCommand(sublime_plugin.TextCommand):
    @_with_session_identifier
    def run(self, _edit, identifier: str, forward: dict) -> None:
        SshForward(
            self.view,
            uuid.UUID(identifier),
            forward["is_reverse"],
            # OpenSSH uses **original** target strings when looking up forwarding channels
            forward["orig_target_1"],
            forward["orig_target_2"],
            do_open=False,
        ).start()

    def is_enabled(self):
        return bool(
            any(
                ssh_session.forwards
                for ssh_session in SshSession.get_all_from_project_data(self.view.window()).values()
            )
        )

    def input(self, _args):
        return _SshSessionInputHandler(
            window=self.view.window(),
            next_input_handler=_SshForwardInputHandler,
            with_forwards=True,
        )

    def input_description(self):
        return "SSH: Cancel forward"


# --- END FORWARDING COMMANDS ---


# --- BEGIN SSHFS COMMANDS ---


class _RemotePathInputHandler(
    sublime_plugin.TextInputHandler, _WithWindowAndSessionCommandInputHandler
):
    def name(self):
        return "remote_path"

    def placeholder(self):
        return "/path/to/remote/folder"

    def validate(self, text):
        return bool(get_absolute_purepath_flavour(text))


class SshOpenDirCommand(sublime_plugin.TextCommand):
    @_with_session_identifier
    def run(self, _edit, identifier: str, remote_path: str):
        SshMountSshfs(
            self.view,
            uuid.UUID(identifier),
            remote_path=typing.cast(PurePath, get_absolute_purepath_flavour(remote_path)),
        ).start()

    def is_enabled(self):
        return bool(SshSession.get_identifiers_from_project_data(self.view.window()))

    def input(self, _args):
        return _SshSessionInputHandler(
            window=self.view.window(), next_input_handler=_RemotePathInputHandler
        )

    def input_description(self):
        return "SSH: Open directory using sshfs"


class _SelectRemotePathInputHandler(
    sublime_plugin.ListInputHandler, _WithWindowAndSessionCommandInputHandler
):
    def __init__(self, *args, current_remote_path: typing.Optional[PurePath] = None, **kwargs):
        super().__init__(*args, **kwargs)

        self.is_first_input = current_remote_path is None
        self.current_remote_path = current_remote_path or self._get_remote_cwd()

    def _get_remote_cwd(self) -> PurePath:
        """
        This method tries to guess remote current working directory, and return it as `PurePath`
        instance object honoring system flavour.
        If remote announces a relative path, defaults to root.
        """
        # fetch remote current working directory (UNIX flavour)
        remote_cwd = ssh_exec(self.identifier, ["pwd"])
        if remote_cwd is not None:
            return get_absolute_purepath_flavour(remote_cwd.rstrip()) or PurePosixPath("/")

        # fetch remote current working directory (Windows flavour)
        remote_cwd = ssh_exec(self.identifier, ["chdir"])
        if remote_cwd is not None:
            return get_absolute_purepath_flavour(remote_cwd.rstrip()) or PureWindowsPath("/")

        # default to POSIX flavour if both commands failed
        return PurePosixPath("/")

    def name(self):
        return "remote_path"

    def preview(self, text):
        return str(self.current_remote_path / (text or ""))

    def description(self, _value, _text):
        return ""

    def list_items(self):
        # special paths acting as sentinels, see `next_input` below
        remote_paths: typing.List[typing.Tuple[str, typing.Optional[str]]] = [
            ("Open current directory", str(self.current_remote_path)),
        ]
        if self.current_remote_path.root != str(self.current_remote_path):
            remote_paths.append(
                ("Go to parent directory", ".."),
            )
        if not self.is_first_input:
            remote_paths.append(
                ("Go back to previous directory", None),
            )

        # list `current_remote_path` sub-directories (/current/remote/path/*/)
        ls_dir_output = ssh_exec(
            self.identifier,
            [
                "ls",
                "-Ad",
                "--",
                # we need to properly quote this path, excluding final glob (to let remote shell
                # expand it).
                # pathlib is also known to strip final separator, but we actually need it here (see
                # <https://bugs.python.org/issue21039>). We infer separator from path flavour.
                shlex.quote(str(self.current_remote_path))
                + "{path_sep}*{path_sep}".format(
                    # pylint: disable=protected-access
                    path_sep=self.current_remote_path._flavour.sep  # type: ignore[attr-defined]
                    # pylint: enable=protected-access
                ),
            ],
        )
        if ls_dir_output is not None:
            remote_paths.extend(
                # `.name` attribute doesn't work with UNC drive names, so we go through `parts`
                (
                    self.current_remote_path.__class__(directory).parts[-1],
                    str(self.current_remote_path / directory),
                )
                for directory in shlex.split(ls_dir_output)
            )

        return remote_paths

    def next_input(self, args):
        remote_path = args[self.name()]
        # user chose _current_ path, stop there
        if remote_path == str(self.current_remote_path):
            return None

        if remote_path is None:  # user wants to return to parent directory
            return sublime_plugin.BackInputHandler()

        if remote_path == "..":
            next_remote_path = self.current_remote_path.parents[0]
        else:
            next_remote_path = self.current_remote_path / remote_path

        # recursively browse the tree according to input
        return self.__class__(
            window=self.window, identifier=self.identifier, current_remote_path=next_remote_path
        )


class SshSelectDirCommand(sublime_plugin.TextCommand):
    @_with_session_identifier
    def run(self, _edit, identifier: str, remote_path: str):
        SshMountSshfs(self.view, uuid.UUID(identifier), remote_path=PurePath(remote_path)).start()

    def is_enabled(self):
        return bool(SshSession.get_identifiers_from_project_data(self.view.window()))

    def input(self, _args):
        return _SshSessionInputHandler(
            window=self.view.window(), next_input_handler=_SelectRemotePathInputHandler
        )

    def input_description(self):
        return "SSH: Select directory using sshfs"


class _SshMountInputHandler(
    sublime_plugin.ListInputHandler, _WithWindowAndSessionCommandInputHandler
):
    def name(self):
        return "mount_path"

    def list_items(self):
        return [
            (remote_path, mount_path)
            for mount_path, remote_path in SshSession.get_all_from_project_data(self.window)[
                str(self.identifier)
            ].mounts.items()
        ]


class SshCloseDirCommand(sublime_plugin.TextCommand):
    @_with_session_identifier
    def run(self, _edit, identifier: str, mount_path: str) -> None:
        SshMountSshfs(
            self.view, uuid.UUID(identifier), do_mount=False, mount_path=Path(mount_path)
        ).start()

    def is_enabled(self):
        return bool(
            any(
                ssh_session.mounts
                for ssh_session in SshSession.get_all_from_project_data(self.view.window()).values()
            )
        )

    def input(self, _args):
        return _SshSessionInputHandler(
            window=self.view.window(),
            next_input_handler=_SshMountInputHandler,
            with_mounts=True,
        )

    def input_description(self):
        return "SSH: Close sshfs directory"


# --- END SSHFS COMMANDS ---


# --- BEGIN TERMINAL COMMAND ---


class SshTerminalCommand(sublime_plugin.TextCommand):
    @_with_session_identifier
    def run(self, _edit, identifier: str):
        # check Terminus third-party package is actually installed before continuing.
        # we check for a (hidden) setting which allows package lookup bypass for developers who know
        # what they're doing
        if not settings.get("terminus_is_installed") and not is_package_installed("Terminus"):
            sublime.error_message("Please install Terminus package to open a remote terminal !")
            return

        window = self.view.window() or sublime.active_window()

        ssh_session = SshSession.get_from_project_data(uuid.UUID(identifier), window)
        title = str(ssh_session) if ssh_session is not None else None

        window.run_command(
            "terminus_open",
            {
                "shell_cmd": shlex.join(
                    get_base_ssh_cmd(
                        uuid.UUID(identifier),
                        ("-q",),
                    )
                ),
                "title": title,
            },
        )

    def is_enabled(self):
        return bool(SshSession.get_identifiers_from_project_data(self.view.window()))

    def input(self, _args):
        return _SshSessionInputHandler(window=self.view.window())

    def input_description(self):
        return "SSH: Open terminal"


# --- END TERMINAL COMMAND ---
