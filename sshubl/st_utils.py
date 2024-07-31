import contextlib
import functools
import getpass
import ipaddress
import itertools
import re
import typing
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

import sublime

try:
    from package_control.package_manager import PackageManager
except ModuleNotFoundError:
    PackageManager = None  # pylint: disable=invalid-name


# hostname regular expression (taken from <https://stackoverflow.com/a/106223>)
HOSTNAME_REGEXP = re.compile(
    r"^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9])$"  # pylint: disable=line-too-long
)

OPENSSH_SPECIAL_BIND_ADDRESSES = ("localhost", "*")


def conditional_cache(no_cache_result: typing.Optional[tuple] = None):
    """A conditional cache wrapper, taken from <https://stackoverflow.com/a/68665480>"""
    if no_cache_result is None:
        no_cache_result = tuple()

    cache: typing.Dict[typing.Tuple[typing.Any, ...], typing.Any] = {}

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _kwargs = tuple(kwargs.items())
            if (args, _kwargs) in cache:
                return cache[(args, _kwargs)]

            res = func(*args, **kwargs)
            if res not in no_cache_result:
                cache[(args, _kwargs)] = res

            return res

        return wrapper

    return decorator


@conditional_cache(no_cache_result=(False,))
def is_package_installed(package_name: str) -> bool:
    """
    This function does its best to check whether a Sublime package is installed or not.
    It lists installed packages using Package Control API (if it's available), with fallback on
    brain-dead iterations through package folders (case-insensitively).

    Once a package has been found, result is cached to prevent unnecessary additional lookups.
    """
    if PackageManager is not None:
        return package_name in PackageManager().list_packages()

    for installed_package in itertools.chain(
        Path(sublime.installed_packages_path()).iterdir(),
        Path(sublime.packages_path()).iterdir(),
    ):
        if package_name.casefold() == installed_package.stem.casefold():
            return True

    return False


@functools.lru_cache()
def format_ip_addr(host: str) -> str:
    """
    This function "encloses" `host` with square brackets if it corresponds to an IPv6 address.
    It also prefers IPv6 addresses "compressed" form, to shorten host strings in display.
    """
    with contextlib.suppress(ValueError):
        ip_address = ipaddress.ip_address(host)
        if ip_address.version == 6:
            return f"[{ip_address.compressed}]"

    return host


@functools.lru_cache()
def get_absolute_purepath_flavour(path: str) -> typing.Optional[PurePath]:
    """
    Return absolute `path` as adequate `PurePath` flavour instance object.
    `None` is returned when `path` is relative (or when not considered absolute in any flavour).
    """
    purepath: PurePath

    purepath = PureWindowsPath(path)
    if purepath.is_absolute():
        return purepath

    purepath = PurePosixPath(path)
    if purepath.is_absolute():
        return purepath

    return None


def parse_ssh_connection(connection_str: str) -> typing.Tuple[str, int, str, typing.Optional[str]]:
    """
    Return a `(host, port, login, password)` tuple from an SSHubl connection string.
    Port defaults to 22 when missing from network location URL part.
    Login defaults to current session username.
    Password will be `None` when it's missing from connection string.

    :raises ValueError: when connection string could not be parsed
    """
    parse_result = urlparse(f"ssh://{connection_str}")
    return (
        parse_result.hostname or "",
        parse_result.port or 22,
        parse_result.username or getpass.getuser(),
        parse_result.password,
    )


def pre_parse_forward_target(forward_str: str) -> typing.Tuple[str, typing.Optional[str]]:
    """
    (pre-)Parse OpenSSH client forward target string.

    This function returns tuple separating "host" and "port", for BSD socket address.
    For UNIX domain socket paths, "port" tuple part will be `None`.

    IPv6 address enclosures are removed from "host" tuple part.
    If port cannot be parsed into integer, "port" tuple part will be `None`.
    """
    parts = forward_str.rsplit(":", maxsplit=1)

    try:
        host, port = parts
    except ValueError:
        # only one part, `forward_str` could be either a port or an UNIX domain socket path
        host, port = forward_str, None
    else:
        # remove square brackets from host (if any)
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]

    return host, port


@functools.lru_cache()
def pretty_forward_target(forward_str: str) -> str:
    """
    Pretty format OpenSSH client forward target string.

    These rules applied :
      * do not alter UNIX domain socket paths
      * do not alter `host:port` BSD socket addresses (when host is a domain name)
      * hide `host` from `host:port` BSD socket addresses, when it's "unspecified" (in RFC terms)
      * hide `host` from `host:port` BSD socket addresses, when it's "loopback" (in RFC terms)
      * hide "OpenSSH special bind addresses" from BSD socket addresses
    """
    host, port = pre_parse_forward_target(forward_str)
    if port is None:
        return host

    try:
        target_ip_address = ipaddress.ip_address(host)
    except ValueError:
        # not an IP address, return only port if host matches known values
        if host in OPENSSH_SPECIAL_BIND_ADDRESSES:
            return port
    else:
        # IP address, return only port if it's considered "loopback" or "unspecified"
        if target_ip_address.is_loopback or target_ip_address.is_unspecified:
            return port

    return forward_str


def validate_forward_target(forward_str: str) -> bool:
    """
    Validate OpenSSH client forward target (either source or destination).

    This function must validate each following forwarding target (-L/-R formats) :
      * port
      * host:port
      * bind_address:port
      * [bind_address_v6]:port
      * socket
    """
    host, port = pre_parse_forward_target(forward_str)
    if port is None:
        return True

    try:
        # allow OpenSSH special "bind addresses" as well as any valid domain name
        # parse `host` as an IP address otherwise
        if host not in OPENSSH_SPECIAL_BIND_ADDRESSES and HOSTNAME_REGEXP.match(host) is None:
            ipaddress.ip_address(host)

        int(port)
    except ValueError:
        return False

    return True
