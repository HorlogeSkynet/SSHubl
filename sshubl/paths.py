from pathlib import Path

import sublime

cache_path = Path(sublime.cache_path()) / "SSHubl"
cache_path.mkdir(parents=True, exist_ok=True)

sockets_path = cache_path / "sockets"
sockets_path.mkdir(mode=0o750, exist_ok=True)

mounts_path = cache_path / "mounts"
mounts_path.mkdir(mode=0o750, exist_ok=True)
