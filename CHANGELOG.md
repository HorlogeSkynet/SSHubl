# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2024-07-31

### Added

- UNIX domain socket automatic removal on forward cancellation

### Changed

- Only hide forward target host part when it corresponds to a "loopback" or "unspecified" IP address in view statuses

### Fixed

- Reverse forward opening when remote target is an UNIX domain socket
- Reverse forward with remote port allocation (e.g. `-R 127.0.0.1:0:[...]`) isn't removed from session on cancellation

## [0.2.1] - 2024-07-14

### Changed

- Project preview
- Disable `pexpect` remote prompt hacks (polluting shell history)

### Fixed

- Remote folder opening when there isn't any currently opened folder

## [0.2.0] - 2024-07-11

### Added

- `edit_settings` command
- Hot reloading for (most of) settings
- Configure Dependabot on GitHub
- Re-connection password prompt cancellation confirmation

### Fixed

- Stop re-connection attempts if user decides to
- Cancellation of reverse forwards with remote port allocation (e.g. `-R 127.0.0.1:0:[...]`)

## [0.1.0] - 2024-07-06

### Added

- Initial release

[Unreleased]: https://github.com/HorlogeSkynet/SSHubl/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/HorlogeSkynet/SSHubl/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/HorlogeSkynet/SSHubl/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/HorlogeSkynet/SSHubl/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/HorlogeSkynet/SSHubl/releases/tag/v0.1.0
