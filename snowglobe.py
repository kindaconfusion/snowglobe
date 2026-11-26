#!/usr/bin/env python3
"""snowglobe: tiny bubblewrap profile manager.

a profile is a generic sandbox (what it can see and touch). run any app in it:

  snowglobe new <profile> [--net] [--x11] [--cli]     make a locked-down profile
  snowglobe run <profile> <command> [args...]         run a command inside it
  snowglobe run <profile>                             no command = shell inside it
  snowglobe run -t <profile> <command>                terminal apps (keeps the tty)
  snowglobe run --dry-run <profile> <command>         print the bwrap command, don't run it
  snowglobe list | show <profile> | edit <profile>
  snowglobe desktop [opts] <profile> <command...>     make a launcher for an app in a profile
  snowglobe rm [--purge] <profile>

profiles live in ~/.config/bwrap-profiles/<profile>.toml
each profile gets one private $HOME in ~/.local/share/bwrap-profiles/<profile>/home,
shared by every app you run in it
"""
import argparse
import getpass
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# on atomic fedora /home is a symlink to /var/home, so work with real paths everywhere
HOME = os.path.realpath(os.path.expanduser("~"))
UID = os.getuid()
XDG_RUN = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{UID}")
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", f"{HOME}/.local/share"))
CONF = Path(os.environ.get("XDG_CONFIG_HOME", f"{HOME}/.config")) / "bwrap-profiles"
DATA = DATA_HOME / "bwrap-profiles"
APPS = DATA_HOME / "applications"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

KNOWN_KEYS = {
    "home", "network", "wayland", "x11", "audio", "gpu", "input", "theme", "tty",
    "dbus", "dbus_talk", "dbus_own", "env_passthrough", "fs", "env",
}
# from the old one-app-per-profile version; ignored now
DEPRECATED_KEYS = {"exec", "args", "name", "icon", "categories"}

# the only bits of /etc the sandbox gets (read-only)
ETC = [
    "passwd", "group", "nsswitch.conf", "host.conf", "hosts", "localtime",
    "machine-id", "os-release", "locale.conf", "mime.types", "ld.so.cache",
    "ld.so.conf", "ld.so.conf.d", "alternatives", "fonts", "ssl", "pki",
    "crypto-policies", "xdg", "vulkan", "glvnd", "OpenCL",
]

# host env vars that survive --clearenv
PASS_ENV = {
    "LANG", "LANGUAGE", "TZ", "TERM", "COLORTERM", "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE", "DESKTOP_SESSION", "XCURSOR_THEME", "XCURSOR_SIZE",
    "QT_QPA_PLATFORM", "QT_QPA_PLATFORMTHEME", "QT_STYLE_OVERRIDE", "GTK_THEME",
    "GDK_BACKEND", "KDE_FULL_SESSION", "KDE_SESSION_VERSION", "SDL_VIDEODRIVER",
    "MOZ_ENABLE_WAYLAND", "ELECTRON_OZONE_PLATFORM_HINT",
}

THEME_PATHS = [
    ".config/gtk-3.0", ".config/gtk-4.0", ".config/kdeglobals",
    ".config/fontconfig", ".local/share/fonts", ".local/share/icons",
    ".themes", ".icons",
]


def warn(msg):
    print(f"snowglobe: {msg}", file=sys.stderr)


def die(msg, code=1):
    warn(msg)
    sys.exit(code)


def expand(p):
    return os.path.expandvars(os.path.expanduser(p))


def parse_bind(spec):
    """'src' or 'src:dest' -> (real src path, dest path)"""
    src, _, dst = spec.partition(":")
    src = os.path.realpath(expand(src))
    return src, (expand(dst) if dst else src)


def check_name(name):
    if not NAME_RE.match(name):
        die(f"bad profile name '{name}' (letters, numbers, . _ - only)")


def load(name):
    check_name(name)
    p = CONF / f"{name}.toml"
    if not p.exists():
        die(f"no profile '{name}' (make one: snowglobe new {name})")
    try:
        prof = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        die(f"{p}: {e}")
    old = sorted(k for k in prof if k in DEPRECATED_KEYS)
    if old:
        warn(f"{p.name}: ignoring {', '.join(old)} (profiles are generic now, "
             f"run apps with: snowglobe run {name} <command>). you can delete those lines")
    for k in prof:
        if k not in KNOWN_KEYS and k not in DEPRECATED_KEYS:
            warn(f"unknown key '{k}' in {p.name} (typo?)")
    return prof


def build(name, prof, cmd, tty=False):
    """returns (bwrap argv, command to run inside, dbus proxy spec or None)"""
    a = ["bwrap", "--unshare-all", "--die-with-parent", "--cap-drop", "ALL", "--clearenv"]
    net = bool(prof.get("network", False))
    if net:
        a += ["--share-net"]
    if not (tty or prof.get("tty", False)):
        a += ["--new-session"]  # blocks TIOCSTI keystroke injection; breaks terminal apps, hence -t / tty = true

    # --- base system, read-only ---
    a += ["--ro-bind", "/usr", "/usr"]
    for link in ("bin", "sbin", "lib", "lib64"):
        if os.path.islink(f"/{link}"):
            a += ["--symlink", os.readlink(f"/{link}"), f"/{link}"]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/var", "--dir", "/var/tmp"]
    if os.path.isdir("/var/usrlocal"):  # /usr/local points here on atomic
        a += ["--ro-bind", "/var/usrlocal", "/var/usrlocal"]
    etc = list(ETC) + (["resolv.conf"] if net else [])
    for e in etc:
        src = os.path.realpath(f"/etc/{e}")
        if os.path.exists(src):
            a += ["--ro-bind", src, f"/etc/{e}"]

    # --- home ---
    if os.path.islink("/home"):
        a += ["--symlink", os.readlink("/home"), "/home"]
    home_mode = prof.get("home", "private")
    if home_mode == "tmpfs":
        a += ["--tmpfs", HOME]
    elif home_mode == "private":
        h = DATA / name / "home"
        h.mkdir(parents=True, exist_ok=True)
        a += ["--bind", str(h), HOME]
    else:
        die("home must be 'private' or 'tmpfs'")
    a += ["--chdir", HOME, "--perms", "0700", "--tmpfs", XDG_RUN]

    # --- env ---
    user = getpass.getuser()
    env = {"HOME": HOME, "USER": user, "LOGNAME": user,
           "PATH": "/usr/local/bin:/usr/bin", "XDG_RUNTIME_DIR": XDG_RUN,
           "SNOWGLOBE": name}  # handy for showing the profile in your shell prompt
    extra_pass = set(prof.get("env_passthrough", []))
    for k, v in os.environ.items():
        if k in PASS_ENV or k in extra_pass or k.startswith("LC_"):
            env[k] = v

    # --- features ---
    if prof.get("wayland", False):
        disp = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        sock = disp if os.path.isabs(disp) else f"{XDG_RUN}/{disp}"
        if os.path.exists(sock):
            base = os.path.basename(sock)
            a += ["--bind", sock, f"{XDG_RUN}/{base}"]
            env["WAYLAND_DISPLAY"] = base
        else:
            warn("wayland = true but no wayland socket found")

    if prof.get("x11", False):
        disp = os.environ.get("DISPLAY", "")
        m = re.match(r"^:(\d+)", disp)
        if m:
            s = f"/tmp/.X11-unix/X{m[1]}"
            a += ["--bind-try", s, s]
            env["DISPLAY"] = disp
            xa = os.environ.get("XAUTHORITY")
            if xa and os.path.exists(xa):
                a += ["--ro-bind", xa, xa]
                env["XAUTHORITY"] = xa
        else:
            warn("x11 = true but DISPLAY isn't set")

    if prof.get("audio", False):
        for s in ("pipewire-0", "pulse/native"):
            a += ["--bind-try", f"{XDG_RUN}/{s}", f"{XDG_RUN}/{s}"]

    sys_paths = []
    if prof.get("gpu", False):
        a += ["--dev-bind-try", "/dev/dri", "/dev/dri"]
        for d in glob.glob("/dev/nvidia*"):
            a += ["--dev-bind", d, d]
        sys_paths += ["/sys/dev/char", "/sys/devices", "/sys/bus/pci"]

    if prof.get("input", False):
        a += ["--dev-bind-try", "/dev/input", "/dev/input", "--ro-bind-try", "/run/udev", "/run/udev"]
        sys_paths += ["/sys/class/input", "/sys/devices"]

    for s in dict.fromkeys(sys_paths):
        a += ["--ro-bind-try", s, s]

    if prof.get("theme", False):
        for rel in THEME_PATHS:
            p = f"{HOME}/{rel}"
            if os.path.exists(p):
                a += ["--ro-bind", os.path.realpath(p), p]

    # --- user-specified binds ---
    for key, flag in (("ro", "--ro-bind-try"), ("rw", "--bind-try"), ("dev", "--dev-bind-try")):
        for spec in prof.get("fs", {}).get(key, []):
            src, dst = parse_bind(spec)
            if os.path.exists(src):
                a += [flag, src, dst]
            else:
                warn(f"skipping missing path: {src}")

    # --- dbus (filtered via xdg-dbus-proxy, which flatpak already pulls in) ---
    proxy = None
    mode = prof.get("dbus", "none")
    if mode == "filtered":
        if not shutil.which("xdg-dbus-proxy"):
            die("dbus = \"filtered\" needs xdg-dbus-proxy")
        addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={XDG_RUN}/bus")
        sock = f"{XDG_RUN}/bwrap-profiles/{name}.bus"
        pcmd = ["xdg-dbus-proxy", addr, sock, "--filter"]
        pcmd += [f"--talk={n}" for n in prof.get("dbus_talk", [])]
        pcmd += [f"--own={n}" for n in prof.get("dbus_own", [])]
        proxy = (pcmd, sock)
        a += ["--bind", sock, f"{XDG_RUN}/bus"]
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={XDG_RUN}/bus"
    elif mode != "none":
        die("dbus must be 'none' or 'filtered'")

    env.update({k: str(v) for k, v in prof.get("env", {}).items()})
    for k, v in env.items():
        a += ["--setenv", k, v]

    # --- the app itself ---
    first = cmd[0]
    path = first if os.path.isabs(first) else shutil.which(first)
    if not path or not os.path.exists(path):
        die(f"can't find '{first}' on the host (flatpaks and shell aliases won't work here)")
    rp = os.path.realpath(path)
    m = re.match(r"^(?:/var)?/opt/([^/]+)", rp)
    if m:  # rpm-ostree'd / vendor apps living in /opt
        a += ["--ro-bind", os.path.realpath(f"/opt/{m[1]}"), f"/opt/{m[1]}"]
    elif rp.startswith(HOME + "/"):
        a += ["--ro-bind", rp, rp]  # app lives in ~ (appimage, ~/.local/bin); add siblings via fs.ro
    return a, [path] + cmd[1:], proxy


def pretty(argv):
    out, line = [], []
    for t in argv:
        if t.startswith("--") and line:
            out.append(" ".join(line))
            line = []
        line.append(shlex.quote(t))
    out.append(" ".join(line))
    return " \\\n  ".join(out)


def cmd_run(a):
    prof = load(a.name)
    rest = a.rest[1:] if a.rest[:1] == ["--"] else a.rest
    tty = a.tty
    if not rest:  # no command: drop into a shell
        sh = os.path.basename(os.environ.get("SHELL", "")) or "bash"
        sh = shutil.which(sh) or shutil.which("bash")
        if not sh:
            die("no shell found, give me a command")
        rest, tty = [sh], True
    argv, cmd, proxy = build(a.name, prof, rest, tty)
    full = argv + cmd
    if a.dry_run:
        if proxy:
            print(shlex.join(proxy[0]) + " &\n")
        print(pretty(full))
        return 0
    if not shutil.which("bwrap"):
        die("bwrap not found (it ships with flatpak, so something is very wrong)")
    if not proxy:
        os.execvp("bwrap", full)
    pcmd, sock = proxy
    Path(sock).parent.mkdir(parents=True, exist_ok=True)
    Path(sock).unlink(missing_ok=True)
    p = subprocess.Popen(pcmd)
    try:
        for _ in range(60):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        else:
            die("xdg-dbus-proxy never made its socket")
        return subprocess.run(full).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        Path(sock).unlink(missing_ok=True)


def template(net, gui, x11):
    b = lambda v: "true" if v else "false"
    return f'''# snowglobe profile: a generic sandbox. run any app in it with:
#   snowglobe run <profile> <command> [args...]      (no command = shell)

home = "private"     # private = persistent fake $HOME shared by apps in this profile | tmpfs = wiped every run
network = {b(net)}
wayland = {b(gui)}
x11 = {b(x11)}         # leaky: x11 apps can snoop on other x11 windows. avoid if you can
audio = {b(gui)}
gpu = {b(gui)}
input = false        # controllers / joysticks
theme = {b(gui)}         # read-only gtk/qt/fonts/icons so apps don't look like ass
tty = {b(not gui)}         # true = terminal apps always work (turns off --new-session). or use: snowglobe run -t

dbus = "none"        # none | filtered (needs xdg-dbus-proxy)
dbus_talk = []       # e.g. ["org.freedesktop.portal.*", "org.freedesktop.Notifications"]
dbus_own = []        # names apps may own, e.g. ["org.mozilla.firefox.*"]

env_passthrough = [] # extra host env vars to let through

[fs]
ro = []    # read-only binds, e.g. ["~/Documents", "~/.config/foo:/some/dest"]
rw = []    # read-write binds, e.g. ["~/Downloads"]
dev = []   # device binds, e.g. ["/dev/video0"]

[env]
# FOO = "bar"
'''


def cmd_new(a):
    check_name(a.name)
    p = CONF / f"{a.name}.toml"
    if p.exists() and not a.force:
        die(f"{p} already exists (--force to overwrite, or: snowglobe edit {a.name})")
    CONF.mkdir(parents=True, exist_ok=True)
    p.write_text(template(a.net, not a.cli, a.x11))
    print(f"wrote {p}")
    print(f"tweak it:  snowglobe edit {a.name}")
    print(f"use it:    snowglobe run {a.name} <command>   (no command = shell inside it)")


def cmd_list(_):
    if not CONF.exists() or not list(CONF.glob("*.toml")):
        print("no profiles yet. snowglobe new <profile>")
        return
    for f in sorted(CONF.glob("*.toml")):
        try:
            p = tomllib.loads(f.read_text())
        except tomllib.TOMLDecodeError:
            print(f"{f.stem:20} (broken toml)")
            continue
        bits = ["net" if p.get("network") else "no-net"]
        bits += [k for k in ("wayland", "x11", "audio", "gpu", "input") if p.get(k)]
        if p.get("dbus", "none") != "none":
            bits.append("dbus:" + p["dbus"])
        print(f"{f.stem:20} {' '.join(bits)}")


def cmd_show(a):
    load(a.name)
    print((CONF / f"{a.name}.toml").read_text(), end="")


def cmd_edit(a):
    load(a.name)
    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not ed:
        ed = next((e for e in ("nano", "vim", "vi", "kate", "gedit") if shutil.which(e)), None)
    if not ed:
        die("no editor found, set $EDITOR")
    os.execvp(shlex.split(ed)[0], shlex.split(ed) + [str(CONF / f"{a.name}.toml")])


def dq(s):
    """quote one arg for a .desktop Exec line (field codes like %U pass through)"""
    if re.fullmatch(r"[\w@%+=:,./-]+", s):
        return s
    return '"' + re.sub(r'([\\"`$])', r"\\\1", s) + '"'


def cmd_desktop(a):
    load(a.name)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        die(f"what should the launcher run? e.g. snowglobe desktop {a.name} firefox")
    me = os.path.realpath(sys.argv[0])
    base = os.path.basename(cmd[0])
    APPS.mkdir(parents=True, exist_ok=True)
    f = APPS / f"snowglobe-{a.name}-{base}.desktop"
    exec_line = " ".join([dq(me), "run"] + (["-t"] if a.terminal else []) + [dq(a.name)] + [dq(c) for c in cmd])
    f.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={a.label or base.capitalize()} ({a.name})\n"
        f"Exec={exec_line}\n"
        f"Icon={a.icon or base}\n"
        f"Categories={a.categories}\n"
        f"Terminal={'true' if a.terminal else 'false'}\n"
        f"X-Snowglobe-Profile={a.name}\n"
    )
    print(f"wrote {f}")


def cmd_rm(a):
    check_name(a.name)
    (CONF / f"{a.name}.toml").unlink(missing_ok=True)
    for f in APPS.glob("snowglobe-*.desktop"):
        try:
            if f"X-Snowglobe-Profile={a.name}\n" in f.read_text():
                f.unlink()
        except OSError:
            pass
    if a.purge:
        shutil.rmtree(DATA / a.name, ignore_errors=True)
        print(f"removed {a.name} and its private home")
    else:
        print(f"removed {a.name} (private home kept in {DATA / a.name}, use --purge to nuke it)")


def main():
    argv = sys.argv[1:]
    cmds = {"new", "run", "list", "ls", "show", "edit", "desktop", "rm", "-h", "--help"}
    if argv and argv[0] not in cmds and NAME_RE.match(argv[0]) and (CONF / f"{argv[0]}.toml").exists():
        die(f"did you mean: snowglobe run {' '.join(argv)}")

    ap = argparse.ArgumentParser(prog="snowglobe", description="tiny bwrap profile manager. "
                                 "usage: snowglobe run <profile> [command...]")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="create a locked-down profile")
    p.add_argument("name")
    p.add_argument("--net", action="store_true", help="allow network")
    p.add_argument("--x11", action="store_true", help="allow x11 (leaky)")
    p.add_argument("--cli", action="store_true", help="terminal apps only, no gui stuff")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("run", help="run a command in a profile (no command = shell)")
    p.add_argument("-t", "--tty", action="store_true", help="terminal app: keep the controlling tty")
    p.add_argument("--dry-run", action="store_true", help="print the bwrap command instead")
    p.add_argument("name")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_run)

    for n, fn in (("list", cmd_list), ("ls", cmd_list)):
        sub.add_parser(n, help="list profiles").set_defaults(fn=fn)
    for n, fn, h in (("show", cmd_show, "print a profile"), ("edit", cmd_edit, "edit a profile")):
        p = sub.add_parser(n, help=h)
        p.add_argument("name")
        p.set_defaults(fn=fn)

    p = sub.add_parser("desktop", help="make a launcher for a command in a profile (options go before the command)")
    p.add_argument("--name", dest="label", help="launcher name (default: the command)")
    p.add_argument("--icon", help="icon name (default: the command)")
    p.add_argument("--categories", default="Utility;")
    p.add_argument("--terminal", action="store_true", help="terminal app")
    p.add_argument("name")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_desktop)

    p = sub.add_parser("rm", help="delete a profile and its launchers")
    p.add_argument("name")
    p.add_argument("--purge", action="store_true", help="also delete its private home")
    p.set_defaults(fn=cmd_rm)

    a = ap.parse_args(argv)
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
