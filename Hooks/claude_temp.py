"""The two roots every script keeps its files under: Claude Code's own tree, and the hooks' state.

Claude Code puts its own temp files (the per-session scratchpad, background task output, tool-result spills) under
raw_claude_root(), CLAUDE_CODE_TMPDIR when set, else %TEMP%\\claude. Every hook and script keeps its state under
claude_root(). On a machine the state share enables (state_share.json), once the account is cut over, that is the one
shared folder every session on every machine reads (D:\\crgolden-state\\claude on the server, Z:\\claude elsewhere); on any
other machine, or before the cutover, it is the raw root. The gate slot is the exception: only one account on one
machine ever uses it, so it stays in account_root(), the account's own local %LOCALAPPDATA%\\Temp\\claude, the same for
every logon session of the account and never on the network.
Paths are returned in their long spelling: on the server TEMP reads C:\\Users\\ADMINI~1\\..., which no allow rule
written as ~/AppData/Local/Temp/claude/** matches.
"""

import ctypes
import json
import os
import re
import shutil
import stat
import tempfile

SHARE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_share.json")
CUTOVER_MARKER = "cutover.json"
LOCAL_CUTOVER_MARKER = "share-cutover.json"


def long_path(path):
    """The path with 8.3 short segments (ADMINI~1) expanded, up to its deepest existing folder for a path not yet
    written, or the path unchanged when it cannot be."""
    if os.name != "nt" or not path:
        return path
    head, missing = path, []
    while not os.path.exists(head):
        parent, name = os.path.split(head)
        if not name or parent == head:
            return path
        missing.append(name)
        head = parent
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetLongPathNameW(head, buffer, len(buffer))
        expanded = buffer.value if 0 < length < len(buffer) else head
    except (AttributeError, OSError):
        return path
    return os.path.join(expanded, *reversed(missing)) if missing else expanded


def root():
    return long_path(os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("TEMP") or tempfile.gettempdir())


def share_config(path=None):
    """The state share's settings, or an empty dict when the file is missing or unreadable."""
    try:
        with open(path or SHARE_CONFIG, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def share_bases(config=None):
    """Every spelling of the share's top folder: the server's local path, the mapped drive, and the UNC path."""
    config = share_config() if config is None else config
    server, share = config.get("server"), config.get("share")
    bases = [config.get("serverPath")]
    if config.get("drive"):
        bases.append(config["drive"].rstrip(":") + ":\\")
    if server and share:
        bases.append("\\\\" + server + "\\" + share)
    return [base for base in bases if base]


def share_root(config=None, machine=None, user=None):
    """The state share's top folder as this machine reaches it (the server's disk path, the mapped drive elsewhere),
    or None when the share does not enable this machine or this account: only the accounts that sign in to the share
    (users) use it, so the triage account on the server keeps its own temp folder."""
    config = share_config() if config is None else config
    machine = (machine or os.environ.get("COMPUTERNAME", "")).upper()
    user = (user if user is not None else os.environ.get("USERNAME", "")).lower()
    if not machine or machine not in {str(name).upper() for name in config.get("enabled", [])}:
        return None
    if user not in {str(name).lower() for name in config.get("users", [])}:
        return None
    if machine == str(config.get("server", "")).upper():
        return config.get("serverPath") or None
    return config.get("drive", "").rstrip(":") + ":\\" if config.get("drive") else None


def cutover_folder(config=None, machine=None, user=None):
    """Where this account's cutover marker sits on the share, <top>\\cutover\\<MACHINE>.<user>, or None. The switch
    stays per account although the state is shared: one account's cutover must never point another account at the
    share before that account's own state has moved."""
    top = share_root(config, machine, user)
    if top is None:
        return None
    machine = (machine or os.environ.get("COMPUTERNAME", "")).upper()
    user = (user if user is not None else os.environ.get("USERNAME", "")).lower()
    return os.path.join(top, "cutover", machine + "." + user)


def share_problem(config=None, machine=None, user=None):
    """Why this account's enabled share cannot be used, or None. A stalled SMB server stalls the check, and the
    dispatcher's onFailure "block" turns Claude Code's hook timeout into a deny, so it never fails open."""
    config = share_config() if config is None else config
    top = share_root(config, machine, user)
    if top is None:
        return None
    if os.path.isdir(top):
        return None
    return ("the state share at {0} does not exist: map {1} to \\\\{2}\\{3} in this sign-in (it reconnects with the "
            "cmdkey entry for {2}), or remove this machine from Harness/Hooks/state_share.json's enabled list").format(
                top, config.get("drive", "Z") + ":", config.get("server", ""), config.get("share", ""))


def raw_claude_root():
    """<root>\\claude: Claude Code's own tree (scratchpad, task output, tool-result spills), always local."""
    return os.path.join(root(), "claude")


FLAT_LAYOUT = "flat"
LEGACY_LAYOUT = "per-machine"
UNKNOWN_LAYOUT = "unknown"


def local_marker_layout(raw_root=None):
    """The layout the raw root's local cutover marker vouches for, or None when it holds none. A marker that names no
    layout was written for the per-machine folder; one that cannot be read is UNKNOWN_LAYOUT, which marked() never
    takes as the per-machine layout, so a damaged marker never forks the state while the share is down."""
    local = os.path.join(raw_root or raw_claude_root(), LOCAL_CUTOVER_MARKER)
    if not os.path.isfile(local):
        return None
    try:
        with open(local, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return UNKNOWN_LAYOUT
    return data.get("layout", LEGACY_LAYOUT) if isinstance(data, dict) else UNKNOWN_LAYOUT


def marked(folder, layout, raw_root=None):
    """Whether Configuration/Move-StateToShare.ps1 has moved this account's state into the given layout on the share,
    judged by the local marker in the raw root first, which never touches the network, so a cut-over account keeps
    resolving to the share while the share is unreachable: a write there fails loudly instead of landing in a second,
    local copy of the state. Only a marker vouching for this layout counts. A raw root that first sees the share's
    marker (a new Remote Desktop session's Temp\\<n>, or a Temp a cleanup emptied) writes its own then, so every root
    that ever reached the share keeps to it."""
    if not folder:
        return False
    found = local_marker_layout(raw_root)
    if found == layout:
        return True
    if not os.path.isfile(os.path.join(folder, CUTOVER_MARKER)):
        top = os.path.dirname(os.path.dirname(folder))
        return found == UNKNOWN_LAYOUT and layout == FLAT_LAYOUT and not os.path.isdir(top)
    local = os.path.join(raw_root or raw_claude_root(), LOCAL_CUTOVER_MARKER)
    if found != FLAT_LAYOUT:
        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, "w", encoding="utf-8") as handle:
                json.dump({"share": folder, "layout": layout}, handle)
        except OSError:
            pass
    return True


def cut_over(folder, raw_root=None):
    """Whether this account's state has moved into the shared flat folder: its own marker under <top>\\cutover, which
    the cutover writes last, so a cutover that stopped partway never points the hooks at a half-filled folder."""
    return marked(folder, FLAT_LAYOUT, raw_root)


def legacy_cut_over(folder, raw_root=None):
    """Whether this account's state sits in the per-machine folder <top>\\<MACHINE>\\<user>\\claude and has not yet
    moved to the flat one, so a new resolver reads the state where it is until the cutover moves it."""
    return marked(folder, LEGACY_LAYOUT, raw_root)


def legacy_folder(config=None, machine=None, user=None):
    """The per-machine share folder <top>\\<MACHINE>\\<user>, or None when the share does not enable this account."""
    top = share_root(config, machine, user)
    if top is None:
        return None
    machine = (machine or os.environ.get("COMPUTERNAME", "")).upper()
    user = (user if user is not None else os.environ.get("USERNAME", "")).lower()
    return os.path.join(top, machine, user)


def account_temp(path):
    """Whether path is this account's own temp folder: %LOCALAPPDATA%\\Temp, or a Remote Desktop session's numbered
    Temp\\<n> under it. A temp folder redirected anywhere else (CLAUDE_CODE_TMPDIR, or TEMP pointed at a fixture) is
    not, so a harness that redirects it never reads or writes the live share."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local or not path:
        return False
    base = os.path.normcase(long_path(os.path.join(local, "Temp"))).rstrip("\\/")
    full = os.path.normcase(long_path(os.path.abspath(path))).rstrip("\\/")
    return full == base or (os.path.dirname(full) == base and os.path.basename(full).isdigit())


def claude_root():
    """Every hook's and script's state: the share's one shared claude folder once the share enables this account and
    its state has been cut over, else the raw root. The share stands in only for the account's own temp folder
    (account_temp)."""
    if not account_temp(root()):
        return raw_claude_root()
    top = share_root()
    if not top:
        return raw_claude_root()
    if cut_over(cutover_folder()):
        return os.path.join(top, "claude")
    legacy = legacy_folder()
    return os.path.join(legacy, "claude") if legacy_cut_over(legacy) else raw_claude_root()


def account_root():
    """The account's own local state, %LOCALAPPDATA%\\Temp\\claude: the same folder for every logon session of the
    account (a Remote Desktop session's TEMP is Temp\\<n>, which would give one account a slot per logon), and never
    the share. A temp folder redirected away from the account's own (account_temp) keeps its raw root, so a harness
    fixture never reaches the live slot."""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local or not account_temp(root()):
        return raw_claude_root()
    return os.path.join(long_path(os.path.join(local, "Temp")), "claude")


def state_spellings(path, config=None):
    """Every spelling of a path under the state share (its local, drive and UNC forms), each in its long form; a path
    outside the share is returned alone."""
    full = os.path.abspath(path)
    forms = [full]
    for base in share_bases(config):
        prefix = base.rstrip("\\") + "\\"
        if os.path.normcase(full).startswith(os.path.normcase(prefix)) or os.path.normcase(full) == os.path.normcase(base.rstrip("\\")):
            rest = full[len(prefix):] if len(full) >= len(prefix) else ""
            forms = [os.path.join(other.rstrip("\\") + "\\", rest) if rest else other.rstrip("\\") for other in share_bases(config)]
            break
    seen, spellings = set(), []
    for form in forms:
        for spelling in (form, long_path(form)):
            key = os.path.normcase(spelling)
            if key not in seen:
                seen.add(key)
                spellings.append(spelling)
    return spellings


def scratch_dir():
    """raw_claude_root(), created if missing: the dir= for every script's tempfile.mkdtemp, TemporaryDirectory and
    NamedTemporaryFile, so no fixture or scratch file lands in the bare temp folder."""
    folder = raw_claude_root()
    os.makedirs(folder, exist_ok=True)
    return folder


def remove_fixture_tree(path):
    """A harness's teardown of its fixture folder: rmtree, clearing a read-only file and retrying it, then OSError
    naming the folder when anything is left, so a fixture that outlives its run fails the harness instead of piling
    up in the temp root unseen."""
    def clear_readonly_and_retry(func, target, _):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if os.path.exists(path):
        shutil.rmtree(path, onexc=clear_readonly_and_retry)
    if os.path.exists(path):
        raise OSError("fixture teardown left {0}".format(path))


def stop_gate_state_root():
    """The Stop gate's per-session state, decisions log and facts log. invoke_main_checkout_gate.py refuses a
    file-tool write under it and the shell gate a command naming it."""
    return os.path.join(claude_root(), "claude-stopgate")


def transcript_index_root():
    """The offsets of the transcript lines each hook reader needs (transcript_index.py). invoke_main_checkout_gate.py
    refuses a file-tool write under it, since an index edited to drop a line hides that line from the hooks."""
    return os.path.join(claude_root(), "transcript-index")


def quarantine_root():
    """The records of sessions ejected for changing the gate that judges them (session_quarantine.py)."""
    return os.path.join(claude_root(), "claude-hooks.quarantine")


def gate_slot_root():
    """Where Harness/Gates/GateSlot.ps1 keeps the account's gate slot, and GateCommon.ps1 each repo's working output:
    account_root(), local to this account on this machine."""
    return os.path.join(account_root(), "crgolden-gates")


def flat_state():
    """Whether this process's state is the share's one shared folder, the only layout whose gate records carry the
    machine in their folder name."""
    return bool(account_temp(root()) and share_root() and cut_over(cutover_folder()))


def checkout_key(checkout, machine=None, flat=None):
    """A checkout's folder name under the gate records: its full long path, lowercased, with every character but a
    letter or digit turned into '-', the way Claude Code names a project folder. In the shared flat folder the name
    starts with the machine, since the four desktops' checkouts share one path (C:\\Users\\crgol\\...); in a local or
    per-machine folder it does not, so an account not yet cut over keeps reading its own records. ClaudeTemp.ps1's
    Get-CheckoutKey returns the same string, so GateCommon writes where gate_status.py reads."""
    full = long_path(os.path.abspath(checkout)).rstrip("\\/").lower()
    if flat if flat is not None else flat_state():
        full = (machine or os.environ.get("COMPUTERNAME", "")).lower() + "-" + full
    return re.sub(r"[^a-z0-9]", "-", full)


def gate_records_root():
    """Every checkout's gate records (<Repo>.<COMPUTERNAME>.last.json and the per-step carries), one folder each,
    in the shared state root."""
    return os.path.join(claude_root(), "crgolden-gates", "records")


def gate_records_dir(checkout):
    """The folder GateCommon.ps1 writes one checkout's gate records in."""
    return os.path.join(gate_records_root(), checkout_key(checkout))


def gate_records_under(folder):
    """The records folder of every checkout inside folder, so a harness that gated fixture checkouts there removes
    their records with the fixture."""
    prefix = checkout_key(folder) + "-"
    root = gate_records_root()
    names = os.listdir(root) if os.path.isdir(root) else []
    return [os.path.join(root, name) for name in names if name.startswith(prefix)]


DELETE_ACCESS = 0x00010000
SHARE_ALL = 7
OPEN_EXISTING = 3
BACKUP_SEMANTICS = 0x02000000
OPEN_REPARSE_POINT = 0x00200000
FILE_DISPOSITION_INFO_EX = 21
DISPOSITION_DELETE_POSIX_IGNORE_READONLY = 0x1 | 0x2 | 0x10


def extended_length_path(path):
    """The \\\\?\\ spelling of an absolute Windows path, which the Win32 file calls accept past MAX_PATH (260
    characters); a path already in that form, or a relative one, is returned unchanged."""
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _unlink_posix(path):
    """Deletes one file or empty directory with POSIX delete semantics, so its name leaves the directory at once even
    while another process (a scanner) still holds it open. Raises OSError on failure."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(ctypes.c_wchar_p(extended_length_path(path)), DELETE_ACCESS, SHARE_ALL, None, OPEN_EXISTING,
                                  BACKUP_SEMANTICS | OPEN_REPARSE_POINT, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        flags = ctypes.c_uint32(DISPOSITION_DELETE_POSIX_IGNORE_READONLY)
        if not kernel32.SetFileInformationByHandle(ctypes.c_void_p(handle), FILE_DISPOSITION_INFO_EX,
                                                   ctypes.byref(flags), ctypes.sizeof(flags)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def remove_tree(path):
    """Deletes a directory tree the way Hooks/ClaudeTemp.ps1 Remove-ClaudeTempTree does: with POSIX delete semantics on
    Windows, since a plain delete of a file a scanner still holds leaves a pending entry and the parent's removal fails
    "The directory is not empty". A junction or symlink is unlinked, never followed. Never raises: it returns the paths
    still present afterwards, empty when the tree is gone, and a caller asserts on that list."""
    spelled = extended_length_path(path) if os.name == "nt" else path
    if not os.path.lexists(spelled):
        return []
    try:
        if os.name == "nt":
            _remove_posix(spelled)
        else:
            import shutil
            shutil.rmtree(spelled)
    except OSError:
        pass
    if not os.path.lexists(spelled):
        return []
    left = [os.path.join(folder, name) for folder, _, names in os.walk(spelled) for name in names]
    return left + [path]


def _remove_posix(path):
    """Deletes every entry it can and keeps going past one that fails, so a single held or denied file leaves only
    itself and its parent folders behind; the folder itself is unlinked last, which fails while anything is left."""
    spelled = extended_length_path(path)
    if os.path.isdir(spelled) and not os.path.islink(spelled) and not os.path.isjunction(spelled):
        for entry in list(os.scandir(spelled)):
            try:
                if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                    _remove_posix(entry.path)
                else:
                    _unlink_posix(entry.path)
            except OSError:
                continue
    _unlink_posix(spelled)


def audits_root():
    """Every audit skill's run state: <audits>\\<skill>\\<run>."""
    return os.path.join(claude_root(), "audits")


def handoffs_root():
    """The session-handoff chains: <handoffs>\\<chain>\\HANDOFF_NNN_<date>.md."""
    return os.path.join(claude_root(), "handoffs")


def legacy_roots(workspace):
    """Every folder outside this root where an earlier layout kept temp files or state, for the workspace given
    (session_home.workspace_root()). Nothing writes to one and nothing reads one: the fleet audit fails a machine that
    still holds any, and test_main_checkout_gate.py fails any other file that names one. ProgramData's are machine-wide,
    so only an elevated session removes them."""
    profile = os.environ.get("USERPROFILE", "")
    local = os.environ.get("LOCALAPPDATA", "")
    temp = long_path(os.environ.get("TEMP") or tempfile.gettempdir())
    program_data = os.environ.get("ProgramData", "")
    roots = []
    if program_data:
        roots += [os.path.join(program_data, name) for name in ("claude-tmp", "crgolden-fleet-setup", "crgolden-gates")]
    if local:
        roots += [os.path.join(local, name) for name in ("crgolden", "crgolden-gates", "crgolden-oneshot")]
    roots += [os.path.join(temp, name) for name in ("claude-stopgate", "crgolden-gates")]
    if profile:
        roots += [os.path.join(profile, ".claude", "audits"), os.path.join(profile, ".claude-master")]
    if workspace:
        roots += [os.path.join(workspace, ".claude", name)
                  for name in ("audits", "gates", "handoffs", "master", "parking-lot", "proposals", "scratch", "work-products")]
        roots += [os.path.join(workspace, ".work-products")]
        roots += [os.path.join(workspace, "Tools", ".claude", name)
                  for name in ("audits", "gates", "parking-lot", "work-products")]
    return roots


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != "legacy-roots":
        sys.stderr.write("usage: claude_temp.py legacy-roots <workspace>\n")
        sys.exit(2)
    sys.stdout.write("\n".join(legacy_roots(sys.argv[2])) + "\n")
