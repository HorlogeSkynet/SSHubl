from pathlib import Path

import sublime


def _settings():
    return sublime.load_settings("SSHubl.sublime-settings")


cache_path = Path(sublime.cache_path()) / "SSHubl"
cache_path.mkdir(parents=True, exist_ok=True)

# OpenSSH binds a temporary UNIX domain socket which is 17 bytes longer than the provided path [1].
# Depending on platform and username, such a path may not fit in kernel pre-allocated space [2]. So
# let's allow users to define their own SSHubl control sockets directory location.
# [1] : <https://github.com/openssh/openssh-portable/blob/5e4bfe6/mux.c#L1285-L1303>
# [2] : <https://unix.stackexchange.com/a/367012>
_sockets_path = _settings().get("sockets_path")
if _sockets_path is not None:
    sockets_path = Path(_sockets_path)
else:
    sockets_path = cache_path / "sockets"
sockets_path.mkdir(mode=0o750, exist_ok=True)

mounts_path = cache_path / "mounts"
mounts_path.mkdir(mode=0o750, exist_ok=True)
