import contextlib
import logging
import os
import platform
import shlex
import shutil
import subprocess
import typing
import uuid
from pathlib import Path, PurePath

try:
    from pexpect import pxssh
except ImportError:
    IS_NONINTERACTIVE_SUPPORTED = False
else:
    IS_NONINTERACTIVE_SUPPORTED = True
import sublime

from .paths import mounts_path, sockets_path
from .st_utils import (
    format_ip_addr,
    is_terminus_installed,
    parse_ssh_connection,
    pre_parse_forward_target,
)
from .vendor import mslex


if platform.system() != "Windows":
    platformlex = shlex
else:
    platformlex = mslex  # type: ignore[misc]


_logger = logging.getLogger(__package__)


def _settings():
    return sublime.load_settings("SSHubl.sublime-settings")


# We double-quote OpenSSH option values to properly deal with white-spaces. From our tests, this is
# the only way to correctly deal with UNIX **and** Windows once escaping has been done.
OPENSSH_OPTION = '-o{0}="{1}"'

ssh_program = _settings().get("ssh_path") or shutil.which("ssh")
sshfs_program = _settings().get("sshfs_path") or shutil.which("sshfs")
umount_program = _settings().get("umount_path")
if platform.system() == "Linux":
    umount_program = umount_program or shutil.which("fusermount")
    umount_flags: typing.Tuple[str, ...] = ("-q", "-u")
else:
    umount_program = umount_program or shutil.which("umount")
    umount_flags = ("-q",)


class PasswordlessConnectionException(Exception):
    """Custom exception raised when password-less authentication failed against server"""


def get_base_ssh_cmd(
    identifier: uuid.UUID,
    args: typing.Tuple[str, ...] = tuple(),
    add_fake_destination: bool = True,
    program_path: typing.Optional[str] = ssh_program,
) -> typing.List[str]:
    if program_path is None:
        raise RuntimeError(f"{program_path} has not been found !")

    base_ssh_cmd = [
        program_path,
        OPENSSH_OPTION.format("ControlPath", sockets_path / str(identifier)),
        # Prevent connection to fake 'destination" if control master is unavailable (inspired by
        # <https://serverfault.com/a/914779>)
        OPENSSH_OPTION.format("ProxyCommand", "exit 1"),
        *args,
    ]

    # OpenSSH CLI requires a 'destination' argument, even when connecting to a master socket
    if add_fake_destination:
        base_ssh_cmd.append("destination")

    return base_ssh_cmd


def get_ssh_master_options(identifier: uuid.UUID) -> dict:
    return {
        **_settings().get("ssh_options", {}),
        # enforce keep-alive for future sshfs usages (see upstream recommendations)
        "ServerAliveInterval": str(_settings().get("ssh_server_alive_interval", 15)),
        "ControlMaster": "auto",
        "ControlPath": str(sockets_path / str(identifier)),
        # keep connection opened for 1 minute (without new connection to control socket)
        "ControlPersist": "60",
    }


def ssh_connect(
    host: str,
    port: int,
    login: str,
    password: typing.Optional[str] = None,
    identifier: typing.Optional[uuid.UUID] = None,
) -> typing.Optional[uuid.UUID]:
    """
    This function connects to host using given credentials (if any) non-interactively using pexpect.
    Connection is made using OpenSSH client, and a control master UNIX socket will be opened to
    allow future channels multiplexing.
    If `identifier` UUID is unset, one will be generated.

    :returns uuid.UUID: session identifier on success (or `None` on error)
    :raises PasswordlessConnectionException: when connection failed due to authentication **and** no
                                             password was given
    """
    if ssh_program is None:
        raise RuntimeError(f"{ssh_program} has not been found !")
    if not IS_NONINTERACTIVE_SUPPORTED:
        raise RuntimeError("Non-interactive connection isn't supported !")

    if identifier is None:
        identifier = uuid.uuid4()

    # run OpenSSH using pexpect to setup connection and non-interactively deal with prompts
    ssh = pxssh.pxssh(options=get_ssh_master_options(identifier))

    # if a password has been given, force password authentication
    if password is not None:
        ssh.force_password = True

    try:
        ssh.login(
            host,
            login,
            password or "",
            login_timeout=_settings().get("ssh_login_timeout", 10),
            port=port,
            auto_prompt_reset=False,
            cmd=ssh_program,
            # allow user to disable host authentication for loopback addresses
            check_local_ip=_settings().get("ssh_host_authentication_for_localhost", True),
        )
    except pxssh.ExceptionPxssh as exception:
        # if authentication failed without password, raise a specific exception
        if password is None and str(exception) in ("password refused", "permission denied"):
            _logger.debug("connection without password failed : %s", str(exception))
            raise PasswordlessConnectionException from None

        _logger.error("ssh connection failed : %s", exception)
        return None

    return identifier


def ssh_connect_interactive(
    connection_str: str,
    identifier: typing.Optional[uuid.UUID] = None,
    mounts: typing.Optional[typing.Dict[str, str]] = None,
    forwards: typing.Optional[typing.List[dict]] = None,
    window: typing.Optional[sublime.Window] = None,
) -> None:
    if ssh_program is None:
        raise RuntimeError(f"{ssh_program} has not been found !")

    if not is_terminus_installed():
        sublime.error_message("Please install Terminus package to connect interactively !")
        return

    if window is None:
        window = sublime.active_window()

    if identifier is None:
        identifier = uuid.uuid4()

    ssh_options = get_ssh_master_options(identifier)
    if not _settings().get("ssh_host_authentication_for_localhost", True):
        ssh_options["NoHostAuthenticationForLocalhost"] = "yes"

    host, port, login, _ = parse_ssh_connection(connection_str)

    terminus_open_args: typing.Dict[str, typing.Any] = {
        "shell_cmd": platformlex.join(
            (
                ssh_program,
                f"-l{login}",
                f"-p{port}",
                *[OPENSSH_OPTION.format(key, value) for key, value in ssh_options.items()],
                host,
            )
        ),
        "title": f"{login}@{format_ip_addr(host)}:{port}",
        "auto_close": "on_success",
        "post_view_hooks": [
            # makes Terminus executes a command which will wait for SSH connection to actually
            # succeed before storing session in project data
            (
                "ssh_interactive_connection_watcher",
                {
                    "identifier": str(identifier),
                    "connection_str": connection_str,
                    "mounts": mounts,
                    "forwards": forwards,
                },
            ),
        ],
    }

    # Development note : please see `SshTerminalCommand` own documentation for below block rationale
    if not _settings().get("honor_spell_check"):
        terminus_open_args["view_settings"] = {
            "spell_check": False,
        }

    window.run_command("terminus_open", terminus_open_args)


def ssh_disconnect(identifier: uuid.UUID) -> None:
    """
    Kill a SSH connection master, causing session graceful disconnection.

    Opened forwards are automatically closed, but sshfs mounts **ARE NOT**, please call
    `umount_sshfs` **beforehand**.
    """

    # delete base mounts path (in a best effort manner) to keep `mounts_path` as clean as possible
    with contextlib.suppress(OSError):
        (mounts_path / str(identifier)).rmdir()

    _logger.debug("killing %s master...", identifier)

    try:
        subprocess.check_call(
            get_base_ssh_cmd(identifier, ("-O", "exit")),
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        # if this fails, we assume session is somehow already down
        _logger.warning("could not request master to exit : %s", (error.stderr or "Unknown error"))


def mount_sshfs(
    identifier: uuid.UUID, remote_path: PurePath, mount_path: typing.Optional[Path] = None
) -> typing.Optional[Path]:
    """
    Mount `remote_path` from `identifier` session using sshfs.
    When `mount_path` is None, an unique mount path relative to session will be generated.

    Some options are passed to sshfs in order to :
        * enable (local) UNIX permissions check
        * follow remote symbolic links
        * map remote user UID/GID to local user

    :returns Path: local mount path on success , or `None` on error
    """
    if mount_path is None:
        mount_path = mounts_path / str(identifier) / f"{remote_path.name}_{uuid.uuid4()}"
    mount_path.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.check_call(
            get_base_ssh_cmd(
                identifier,
                (
                    # enable local permissions check
                    "-odefault_permissions",
                    # follow symlinks on the server
                    "-ofollow_symlinks",
                    # map remote user UID/GID to current user
                    "-oidmap=user",
                    f"-ouid={os.getuid()}",
                    f"-ogid={os.getgid()}",
                    # fake 'destination' (see `get_base_ssh_cmd` for rationale)
                    f"destination:{remote_path}",
                    str(mount_path),
                ),
                add_fake_destination=False,
                program_path=sshfs_program,
            ),
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        _logger.error(
            "could not mount %s over sshfs : %s",
            remote_path,
            (error.stderr or "Unknown error").rstrip(),
        )

        # delete prepared directory as mounting operation failed
        with contextlib.suppress(FileNotFoundError):
            mount_path.rmdir()

        return None

    return mount_path


def umount_sshfs(mount_path: Path) -> None:
    if umount_program is None:
        _logger.warning(
            "%s has not been found, skipping unmounting of %s...", umount_program, mount_path
        )
        return

    _logger.debug("unmounting %s...", mount_path)

    try:
        subprocess.check_call(
            [umount_program, *umount_flags, str(mount_path)],
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        _logger.warning(
            "could not unmount %s : %s", mount_path, (error.stderr or "Unknown error").rstrip()
        )
    else:
        _logger.debug("successfully unmounted %s, removing mount point...", mount_path)

    with contextlib.suppress(FileNotFoundError):
        mount_path.rmdir()


def ssh_forward(
    identifier: uuid.UUID, do_open: bool, is_reverse: bool, target_1: str, target_2: str
) -> typing.Optional[dict]:
    """
    Open/Close (reverse) port/UNIX domain socket forwarding, and return a dict uniquely identifying
    it on success.
    If an error occurs, it is logged and `None` is returned.
    """
    if is_reverse:
        target_local, target_remote = target_2, target_1
    else:
        target_local, target_remote = target_1, target_2

    try:
        stdout = subprocess.check_output(
            get_base_ssh_cmd(
                identifier,
                (
                    "-O",
                    "forward" if do_open else "cancel",
                    "-R" if is_reverse else "-L",
                    f"{target_1}:{target_2}",
                ),
            ),
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        _logger.error(
            "could not %s forward %s %s %s : %s",
            "open" if do_open else "close",
            target_local,
            "<-" if is_reverse else "->",
            target_remote,
            (error.stderr or "Unknown error").rstrip(),
        )
        return None

    # if port is expected to be dynamically-allocated by remote, update `target_remote` to allow
    # future forward requests to re-use the same port
    target_1_host, target_1_port_str = pre_parse_forward_target(target_1)
    try:
        target_1_port = int(target_1_port_str or "")
    except ValueError:
        target_1_port = None
    if do_open and is_reverse and target_1_port == 0:
        try:
            remote_port = int(stdout)
        except ValueError:
            _logger.warning("could not retrieve port allocated by remote from : %s", stdout)
        else:
            _logger.debug(
                "remote successfully allocated %d for reverse forward to %s",
                remote_port,
                target_local,
            )
            target_remote = f"{target_1_host}:{remote_port}"

    # when closing an UNIX domain socket forward, also remove socket from disk to allow future
    # forward requests to re-use the same path
    if not do_open and target_1_port is None:
        if is_reverse:
            unix_socket_path = shlex.quote(target_1)
            if (
                ssh_exec(identifier, ("rm", "-f", unix_socket_path)) is None
                and ssh_exec(identifier, ("del", "/q", unix_socket_path)) is None
            ):
                _logger.warning("couldn't remove remote UNIX domain socket : %s", unix_socket_path)
        else:
            Path(target_1).unlink(missing_ok=True)

    _logger.debug(
        "successfully %s forward %s %s %s",
        "opened" if do_open else "closed",
        target_local,
        "<-" if is_reverse else "->",
        target_remote,
    )
    return {
        "is_reverse": is_reverse,
        "orig_target_1": target_1,
        "orig_target_2": target_2,
        "target_local": target_local,
        "target_remote": target_remote,
    }


def ssh_exec(identifier: uuid.UUID, args: typing.Iterable[str]) -> typing.Optional[str]:
    """
    Execute `args` command remotely using a non-interactive pseudo-TTY.
    `args` arguments **ARE NOT** escaped.
    """
    try:
        stdout = subprocess.check_output(
            [
                *get_base_ssh_cmd(
                    identifier,
                    # force PTY allocation as we may execute a command that requires one
                    ("-q", "-tt"),
                ),
                "--",
                # pass remote command to OpenSSH as an unique argument
                " ".join(args),
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        _logger.debug(
            "executing %s on remote failed with %d : %s",
            list(args),
            error.returncode,
            (error.stderr or "Unknown error").rstrip(),
        )
        return None

    _logger.debug("successfully executed %s remotely : stdout=%s", args, stdout.rstrip())
    return stdout


def ssh_check_master(identifier: uuid.UUID) -> bool:
    try:
        subprocess.check_call(
            get_base_ssh_cmd(identifier, ("-O", "check")),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return False

    return True
