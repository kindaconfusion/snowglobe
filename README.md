# snowglobe

snowglobe is a small Bubblewrap profile manager for running applications in restricted sandboxes.

## Requirements

- Linux
- [`bwrap`](https://github.com/containers/bubblewrap)
- Python 3.11 or newer
- `xdg-dbus-proxy` for profiles with filtered D-Bus access

## Quick start

Create a profile:

```sh
snowglobe new [profile]
```

Run an application in the profile:

```sh
snowglobe run [profile] [application]
```

Without a command, Snowglobe opens a shell:

```sh
snowglobe run [profile]
```

## Commands

| Command | Description |
| --- | --- |
| `snowglobe new <profile> [--net] [--x11] [--cli] [--force]` | Create a profile |
| `snowglobe run <profile> [command ...]` | Run a command, or open a shell |
| `snowglobe run -t <profile> <command>` | Run a terminal application and keep the TTY |
| `snowglobe run --dry-run <profile> <command>` | Print the Bubblewrap command without running it |
| `snowglobe list` | List profiles |
| `snowglobe show <profile>` | Print a profile |
| `snowglobe edit <profile>` | Edit a profile |
| `snowglobe desktop [options] <profile> <command ...>` | Create a desktop launcher |
| `snowglobe rm [--purge] <profile>` | Remove a profile |

`--purge` also removes the profile's private home directory. By default, snowglobe keeps that directory.

## Profiles

Profiles are TOML files stored in `~/.config/bwrap-profiles/<profile>.toml`.
Each profile has a persistent private home at `~/.local/share/bwrap-profiles/<profile>/home`, shared by every application run in that profile.

Set `home = "tmpfs"` to use a temporary home that is discarded after each run.

```toml
home = "private"
network = true
wayland = true
x11 = false
audio = true
gpu = true
input = false
theme = true
tty = false

dbus = "none"
dbus_talk = []
dbus_own = []
env_passthrough = []

[fs]
ro = ["~/Documents"]
rw = ["~/Downloads"]
dev = []

[env]
# FOO = "bar"
```

Available bind types are:

- `ro`: read-only bind
- `rw`: read-write bind
- `dev`: device bind

The optional source and destination can be separated with `:`:

```toml
[fs]
ro = ["~/Documents", "~/.config/example:/config/example"]
rw = ["~/Downloads"]
dev = ["/dev/video0"]
```

For filtered D-Bus access, set `dbus = "filtered"` and list the names that the application may talk to or own:

```toml
dbus = "filtered"
dbus_talk = ["org.freedesktop.portal.*"]
dbus_own = ["org.example.MyApp.*"]
```