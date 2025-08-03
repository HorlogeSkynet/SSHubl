# SSHubl

> A Sublime Text 4+ plugin for your SSH connections

<p align="center">
	<a href="https://packagecontrol.io/packages/SSHubl"><img src="https://img.shields.io/packagecontrol/dm/SSHubl?style=for-the-badge"></a>
</p>

## Introduction

This plugin aims to grant the power of (Open)SSH to Sublime Text. Included features are :

* Open a remote terminal
* Open a remote folder over sshfs
* Open forward and reverse ports (or UNIX domain sockets)
* Automatic environment re-setup on [project](https://www.sublimetext.com/docs/projects.html) opening

It has been inspired by Visual Studio Code [Remote - SSH](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-ssh) plugin, **without** the drawback of depending on a remote agent running on the SSH server.

## Dependencies

* Sublime Text 4081+
* OpenSSH client
* sshfs (FUSE) client
* pexpect Python package (used for non-interactive SSH connection on Linux/macOS)
* [Terminus](https://packagecontrol.io/packages/Terminus) Sublime Text package (used for remote terminal feature on Linux/macOS, **required** on Windows)

On Debian : `apt-get install -y sshfs`

## Installation


### With Package Control (recommended)

1. Open your command palette and type in : `Package Control: Install Package`
2. Browse the list or search for `SSHubl`
3. Press `Enter` and you're done !

Package Control dedicated page [here](https://packagecontrol.io/packages/SSHubl).


### Manually

1. Go to the Sublime Text packages folder (usually `$HOME/.config/sublime-text/Packages/` or `%AppData%\Sublime Text\Packages\`)
2. Clone this repository there : `git clone https://github.com/HorlogeSkynet/SSHubl.git`
3. \[Linux/macOS\] Satisfy either `pexpect` and `ptyprocess` third-party dependencies in Sublime Text `Lib/python38/` folder (see [here](https://stackoverflow.com/a/61200528) for further information) or [Terminus](https://packagecontrol.io/packages/Terminus) Sublime Text package dependency
4. \[Windows\] Satisfy [Terminus](https://packagecontrol.io/packages/Terminus) Sublime Text package dependency
5. Restart Sublime Text and... :tada:

## Usage

Open your command palette and type in `SSHubl` to select `Connect to server`. Once connected, you will be able to select `Forward port/socket`, `Open/Select directory (mount sshfs)` or even `Open a terminal` commands.

![Preview](https://i.imgur.com/i5uPoWD.gif)

## Settings

```javascript
{
	"debug": false,
	// Custom path to OpenSSH client program
	// /!\ This setting requires plugin reload (or Sublime restart)
	"ssh_path": null,
	// Custom path to `sshfs` FUSE client program
	// /!\ This setting requires plugin reload (or Sublime restart)
	"sshfs_path": null,
	// Custom path to `umount` program (`fusermount` on Linux)
	// /!\ This setting requires plugin reload (or Sublime restart)
	"umount_path": null,
	// Custom path to OpenSSH control sockets directory
	// /!\ This setting requires plugin reload (or Sublime restart)
	// If you hit "path [...] too long for Unix domain socket" error, you may set this to e.g. "/tmp/sshubl"
	"sockets_path": null,
	// Custom options to pass to OpenSSH **master** (e.g. useful for bastion traversal)
	"ssh_options": {
		//"ConnectTimeout": 30,
	},
	// Custom login timeout (for pexpect)
	"ssh_login_timeout": 10,
	// Set to `false` to disable host authentication for loopback addresses (cf. NoHostAuthenticationForLocalhost)
	"ssh_host_authentication_for_localhost": true,
	// Server keepalive interval (as recommended in sshfs documentation)
	"ssh_server_alive_interval": 15,
}
```

## Frequently Asked Questions

### Why can I non-interactively connect to new hosts without accepting their fingerprint ?

> `pexpect` package is [known to always accept remotes' public key](https://github.com/pexpect/pexpect/blob/4.9/pexpect/pxssh.py#L411-L414), and it isn't configurable.

### How is "SSHubl" pronounced ?

> \[ʃʌbəl\]

### Why haven't you opted for a pure Python approach ?

> Paramiko doesn't support FUSE. There is also `fs.sshfs`, but it relies on PyFilesystem 2 which doesn't support "re-exposing" FUSE as local mount point.

### Is SSHubl compatible with other SSH clients ?

> As it uses OpenSSH connections multiplexing feature, no.

### Do you plan to support Sublime Text 3 ?

> It's very unlikely as SSHubl requires Python 3.8 runtime and depends on several Sublime Text 4081+ API.
