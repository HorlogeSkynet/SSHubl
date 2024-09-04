import dataclasses
import typing
import uuid
from threading import RLock as ThreadingLock

import sublime

from .st_utils import format_ip_addr, pretty_forward_target

lock = ThreadingLock()


def update_window_status(window: typing.Optional[sublime.Window] = None):
    if window is None:
        window = sublime.active_window()

    # build a set of SSH sessions which will accumulates forwards
    forwards_per_unique_ssh_sessions: typing.Dict[str, typing.List[dict]] = {}
    for ssh_session in SshSession.get_all_from_project_data(window).values():
        # mask "down" sessions from status
        if not ssh_session.is_up:
            continue

        forwards_per_unique_ssh_sessions.setdefault(str(ssh_session), []).extend(
            ssh_session.forwards
        )

    # build forwards status strings (comma-separated)
    ssh_sessions_with_forwards_status = [
        (
            ssh_session,
            ", ".join(
                f"{pretty_forward_target(forward['target_local'])} "
                f"{'<-' if forward['is_reverse'] else '->'} "
                f"{pretty_forward_target(forward['target_remote'])}"
                for forward in forwards
            ),
        )
        for ssh_session, forwards in forwards_per_unique_ssh_sessions.items()
    ]

    # build final SSH sessions status string (pipe-separated, with enclosed forwards status strings)
    ssh_sessions_status = "SSH : " + " | ".join(
        ssh_session + (f" [ {forwards_status} ]" if forwards_status else "")
        for ssh_session, forwards_status in ssh_sessions_with_forwards_status
    )

    for view in window.views():
        if ssh_sessions_with_forwards_status:
            view.set_status("sshubl_status", ssh_sessions_status)
        else:
            view.erase_status("sshubl_status")


def add_to_project_folders(
    new_folder: str,
    sidebar_name: str,
    window: typing.Optional[sublime.Window] = None,
) -> None:
    """
    There is currently no simple way to programmatically add a folder to current "project".
    See <https://forum.sublimetext.com/t/add-a-folder-to-sidebar-via-api/3812> and
    <https://forum.sublimetext.com/t/how-to-programmatically-open-a-new-window-with-given-folder/26894>.
    """
    if window is None:
        window = sublime.active_window()

    with lock:
        project_data = window.project_data() or {}
        folders = project_data.get("folders", [])
        if new_folder not in {folder["path"] for folder in folders}:
            folders.append({"path": new_folder, "name": sidebar_name})
            project_data["folders"] = folders
            window.set_project_data(project_data)

    # make Sublime refresh folders list, as we might haven't triggered `set_project_data` and folder
    # may correspond to a remote mount point which wasn't ready when it first started
    window.run_command("refresh_folder_list")


def remove_from_project_folders(
    old_folder: str,
    window: typing.Optional[sublime.Window] = None,
) -> None:
    """Mirror function of `add_folder_to_project` (see above)"""
    if window is None:
        window = sublime.active_window()

    with lock:
        project_data = window.project_data() or {}
        folders = project_data.get("folders", [])
        filtered_folders = [folder for folder in folders if folder["path"] != old_folder]
        if filtered_folders != folders:
            project_data["folders"] = filtered_folders
            window.set_project_data(project_data)


@dataclasses.dataclass
class SshSession:  # pylint: disable=too-many-instance-attributes
    identifier: str
    host: str
    port: int
    login: str
    mounts: typing.Dict[str, str] = dataclasses.field(default_factory=dict)
    forwards: typing.List[typing.Dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    is_interactive: bool = False
    is_up: typing.Optional[bool] = True

    def __str__(self) -> str:
        return f"{self.login}@{format_ip_addr(self.host)}:{self.port}"

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def is_same_forward(forward_1: dict, forward_2) -> bool:
        """
        This method implements a forwarding rules comparison (with only fields that matter, see
        `_get_all_raw` below for format documentation).
        """
        return (
            forward_1["is_reverse"],
            forward_1["orig_target_1"],
            forward_1["orig_target_2"],
        ) == (
            forward_2["is_reverse"],
            forward_2["orig_target_1"],
            forward_2["orig_target_2"],
        )

    @classmethod
    def _get_all_raw(cls, window: typing.Optional[sublime.Window] = None) -> typing.Dict[str, dict]:
        if window is None:
            window = sublime.active_window()

        return (
            (window.project_data() or {})
            .get("SSHubl", {})
            .get(
                "sessions",
                {
                    # Development notes : SSH sessions format in project data is detailed below
                    #
                    # <session_uuid>: {
                    #     "host": "",
                    #     "port": 22,
                    #     "login": "",
                    #     "mounts": {
                    #         "/local/mount/point": "/remote/path",
                    #     },
                    #     "forwards": [
                    #         // we save original forward targets as OpenSSH expects them on
                    #         // cancellation (see `SshCancelForwardCommand`)
                    #         {
                    #             // "-L 127.0.0.1:8888:127.0.0.1:22" would be stored as :
                    #             "is_reverse": false,
                    #             "orig_target_1": "127.0.0.1:8888", // 1st forward target
                    #             "orig_target_2": "127.0.0.1:22",   // 2nd forward target
                    #             "target_local": "127.0.0.1:8888",  // "local" forward target
                    #             "target_remote": "127.0.0.1:22",   // "remote" forward target
                    #         },
                    #         {
                    #             // "-R 127.0.0.1:0:[::1]:8888" would be stored as :
                    #             "is_reverse": true,
                    #             "orig_target_1": "127.0.0.1:0",
                    #             "orig_target_2": "[::1]:8888",
                    #             "target_local": "[::1]:8888",
                    #             "target_remote": "127.0.0.1:4242",  // allocated by remote
                    #         },
                    #     ],
                    #     "is_interactive": false,
                    #     "is_up": true,
                    # },
                },
            )
        )

    @classmethod
    def get_identifiers_from_project_data(
        cls, window: typing.Optional[sublime.Window] = None
    ) -> typing.Tuple[str, ...]:
        return tuple(cls._get_all_raw(window).keys())

    @classmethod
    def get_all_from_project_data(
        cls, window: typing.Optional[sublime.Window] = None
    ) -> typing.Dict[str, "SshSession"]:
        return {
            identifier: cls(**ssh_session)
            for identifier, ssh_session in cls._get_all_raw(window).items()
        }

    @classmethod
    def get_from_project_data(
        cls,
        identifier: uuid.UUID,
        window: typing.Optional[sublime.Window] = None,
    ) -> typing.Optional["SshSession"]:
        return cls.get_all_from_project_data(window).get(str(identifier))

    def set_in_project_data(self, window: typing.Optional[sublime.Window] = None) -> None:
        if window is None:
            window = sublime.active_window()

        with lock:
            project_data = window.project_data() or {}
            project_data.setdefault("SSHubl", {}).setdefault("sessions", {})[self.identifier] = (
                self.as_dict()
            )
            window.set_project_data(project_data)

    def remove_from_project_data(self, window: typing.Optional[sublime.Window] = None) -> None:
        if window is None:
            window = sublime.active_window()

        with lock:
            ssh_sessions = self._get_all_raw(window)
            if ssh_sessions.pop(self.identifier, None) is not None:
                project_data = window.project_data() or {}
                project_data.setdefault("SSHubl", {})["sessions"] = ssh_sessions
                window.set_project_data(project_data)
