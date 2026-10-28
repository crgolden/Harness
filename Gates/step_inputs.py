"""Tools' gate plan: every harness step, found by naming convention, with the files its harness reaches as its inputs,
worked out from source, so no hand-kept list of steps or inputs can go stale (Gates/test_step_inputs.py).

A harness is a tracked file named Test-*.ps1 or test_*.py, or any .py or .ps1 whose header carries a GATE-STEP marker.
The marker, a line at column 0 in the first lines of the file ("# GATE-STEP: ..."), names the step and how it runs:

    GATE-STEP: <step name>                   the file is run as python <file> or pwsh -File <file>
    GATE-STEP: <step name> | args: a b       the same, with those arguments
    GATE-STEP: <step name> | pytest          python -m pytest -x -q over the file's folder
    GATE-STEP: none | <why>                  the file is not a gate step (a live check, a data script)

A conventionally named file with no marker is a step named from its file name ("test_repos_audit.py" runs as
"repos audit harness").

A harness reaches a file by importing a module that sits beside it or in a folder it puts on sys.path, by
dot-sourcing a PowerShell script, or by naming a repo file in a path it builds from its own location or the repo's
(a constant, os.path.join over __file__, a pathlib parent, $PSScriptRoot or Join-Path). Every file reached that way
is followed in turn, except a file the code only reads as text (open, read_text, Get-Content, [IO.File]::Read*):
that file is an input, but what it names is not. A bare file name counts only beside the file naming it, so a scanner looking for any AGENTS.md
reaches none. A directory listing in a harness file widens its step instead: a GATE-LISTING line in that harness
("# GATE-LISTING: <text in the listing's line> | <folder globs, or none>") classifies it, one rooted in a temp folder adds nothing, one rooted at the harness's own folder adds that folder, and any other makes
the step owe every edit (inputs '*') with a warning naming the line. Nothing the analysis cannot follow fails a gate;
it only costs that step its carry.

    python Harness/Gates/step_inputs.py <repo root> --plan [--cache <file>]

prints the plan as JSON: {"steps": [{"name", "harness", "command", "inputs"}], "warnings": [...]}, the command with
absolute paths. Each file's analysis is kept in the cache keyed on its content hash and the tracked file list, so a
plan after a one-file edit reads only that file again.

    python Harness/Gates/step_inputs.py <repo root> <harness> [<harness> ...]

prints one line per file a harness reaches, repo-relative with forward slashes, then a DYNAMIC: line per listing.
"""

import ast
import fnmatch
import hashlib
import json
import os
import re
import sys

SOURCE_SUFFIXES = (".py", ".ps1", ".psm1")
DATA_SUFFIXES = (".json", ".txt", ".md", ".html", ".mjs", ".js", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".sql")
RE_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
RE_PS_STRING = re.compile(r"'([^'\r\n]{3,})'|\"([^\"\r\n]{3,})\"")
RE_PS_VARIABLE_PATH = re.compile(r"\$\{?(\w+)\}?[\\/]((?:[\w.\- ]+[\\/])*[\w.\-]+\.\w+)")
RE_PS_RUNS = re.compile(r"(?:^|[\s(;|])(?:\.|&)\s+[\(\"'$]|\bpwsh\b|\bInvoke-Expression\b|\bStart-Process\b", re.IGNORECASE)
RE_PS_DYNAMIC = re.compile(r"\bGet-ChildItem\b[^\r\n]*-(?:Recurse|Filter)|\[IO\.Directory\]::Get(?:Files|Directories)")
PY_DYNAMIC_CALLS = {("os", "listdir"), ("os", "scandir"), ("os", "walk"), ("glob", "glob"), ("glob", "iglob"),
                    ("importlib", "import_module")}
PATH_FUNCTIONS = {"dirname", "abspath", "realpath", "normpath", "join", "expanduser"}


def rel(root, path):
    return os.path.relpath(path, root).replace("\\", "/")


def inside(root, path):
    return os.path.normcase(os.path.normpath(path)).startswith(os.path.normcase(os.path.normpath(root)) + os.sep)


def resolve_literal(root, here, text):
    """The repo file a string literal names, else None. A bare file name is the file of that name beside the one
    naming it; a path is tried against that file's folder, its parent and the repo root."""
    candidate = text.replace("\\", "/").strip().strip("/")
    if not candidate or "*" in candidate or ":" in candidate or "{" in candidate or "$" in candidate:
        return None
    if not candidate.lower().endswith(SOURCE_SUFFIXES + DATA_SUFFIXES):
        return None
    bases = (here,) if "/" not in candidate else (here, os.path.dirname(here), root)
    for base in bases:
        full = os.path.normpath(os.path.join(base, candidate))
        if inside(root, full) and os.path.isfile(full):
            return full
    return None


class PathEvaluator:
    """Evaluates the path expressions a Python file builds from __file__ and constants, so a path joined in parts
    (os.path.join(HERE, "..", "Hooks", "x.py"), Path(__file__).parent / "x.py") still names its file."""

    def __init__(self, path, tree):
        self.names = {"__file__": path}
        assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign) and len(node.targets) == 1
                       and isinstance(node.targets[0], ast.Name)]
        for _ in range(4):
            for node in assignments:
                value = self.value(node.value)
                if value is not None:
                    self.names.setdefault(node.targets[0].id, value)

    def value(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self.value(node.left), self.value(node.right)
            return os.path.join(left, right) if left is not None and right is not None else None
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            inner = self.value(node.value)
            return os.path.dirname(inner) if inner is not None else None
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in ("resolve", "absolute") and isinstance(node.func, ast.Attribute):
                return self.value(node.func.value)
            args = [self.value(arg) for arg in node.args]
            if name in ("Path", "PurePath") and len(args) == 1:
                return args[0]
            if name in PATH_FUNCTIONS and args and all(arg is not None for arg in args):
                if name == "join":
                    return os.path.join(*args)
                if name == "dirname":
                    return os.path.dirname(args[0])
                return os.path.normpath(os.path.abspath(args[0]) if name in ("abspath", "realpath") else args[0])
        return None


PY_READ_CALLS = {"open", "read_text", "read_bytes"}


def python_read_nodes(tree):
    """The ids of every AST node that only names a file the code reads as text: anything inside the arguments of
    open(), or the receiver of .read_text()/.read_bytes(), and the value of a name assigned once and loaded only in
    such places. A file read is an input, but what it names is not followed."""
    inside, loads = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in PY_READ_CALLS:
                parts = list(node.args) + ([node.func.value] if isinstance(node.func, ast.Attribute) else [])
                for part in parts:
                    inside.update(id(child) for child in ast.walk(part))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads.setdefault(node.id, []).append(node)
    read = set(inside)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            uses = loads.get(node.targets[0].id, [])
            if uses and all(id(use) in inside for use in uses):
                read.update(id(child) for child in ast.walk(node.value))
    return read


def python_reach(root, path):
    """(files, dynamic, read) one Python file reaches directly; read holds the files it only reads as text."""
    here = os.path.dirname(path)
    with open(path, encoding="utf-8", errors="replace") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return set(), [(path, error.lineno or 0, "does not parse: {0}".format(error.msg))], set()
    evaluator = PathEvaluator(path, tree)
    read_nodes = python_read_nodes(tree)
    search = [here]
    files, dynamic, read = set(), [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("insert", "append") and \
                getattr(getattr(node.func, "value", None), "attr", None) == "path":
            for arg in node.args:
                folder = evaluator.value(arg)
                if isinstance(folder, str) and os.path.isdir(os.path.normpath(os.path.join(here, folder))):
                    search.append(os.path.normpath(os.path.join(here, folder)))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module.split(".")[0]]
        for name in names:
            for folder in search:
                module = os.path.join(folder, name + ".py")
                if os.path.isfile(module):
                    files.add(os.path.normpath(module))
                    break
        if isinstance(node, (ast.Call, ast.BinOp, ast.Attribute)):
            built = evaluator.value(node)
            if isinstance(built, str) and os.path.isabs(built):
                full = os.path.normpath(built)
                if inside(root, full) and os.path.isfile(full):
                    (read if id(node) in read_nodes else files).add(full)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found = resolve_literal(root, here, node.value)
            if found:
                (read if id(node) in read_nodes else files).add(found)
            elif RE_MODULE_NAME.match(node.value):
                for folder in search:
                    module = os.path.join(folder, node.value + ".py")
                    if os.path.isfile(module):
                        files.add(os.path.normpath(module))
                        break
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and (node.func.value.id, node.func.attr) in PY_DYNAMIC_CALLS:
            dynamic.append((path, node.lineno))
    lines = source.splitlines()
    return files - read, [(where, number, lines[number - 1].strip()) for where, number in dynamic], read - files


RE_PS_READ = re.compile(r"\bGet-Content\b|\[IO\.File\]::Read\w*\(", re.IGNORECASE)


def powershell_reach(root, path):
    """(files, dynamic, read) one PowerShell file reaches directly; a file named on a line that reads it as text
    (Get-Content, [IO.File]::Read*) and runs nothing there is read, not followed."""
    here = os.path.dirname(path)
    with open(path, encoding="utf-8", errors="replace") as handle:
        source = handle.read()
    files, dynamic, read = set(), [], set()
    for number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        target = read if RE_PS_READ.search(line) and not RE_PS_RUNS.search(line) else files
        for match in RE_PS_STRING.finditer(line):
            found = resolve_literal(root, here, match.group(1) or match.group(2))
            if found:
                target.add(found)
        for match in RE_PS_VARIABLE_PATH.finditer(line):
            tail = match.group(2)
            for base in (here, root):
                full = os.path.normpath(os.path.join(base, tail))
                if inside(root, full) and os.path.isfile(full):
                    target.add(full)
                    break
        if RE_PS_DYNAMIC.search(line):
            dynamic.append((path, number, line.strip()))
    return files, dynamic, read - files


def tracked_case(root):
    """{lowercased repo-relative path: the path as git tracks it}, so a literal naming a file in another case still
    names the tracked file once."""
    import subprocess
    listed = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard"], capture_output=True,
                            text=True).stdout.splitlines()
    return {line.lower(): line for line in listed if line}


CACHE_VERSION = 2


class Analysis:
    """What each file of one repo reaches directly, read once per run and kept on disk between runs, keyed on the
    file's content hash and on the tracked file list (a file added or removed can change what a literal names)."""

    def __init__(self, root, casing, cache_path=None):
        self.root = root
        self.casing = casing
        self.path = cache_path
        self.tree = hashlib.sha256("\n".join(sorted(casing.values())).encode("utf-8")).hexdigest()
        self.entries = {}
        self.memo = {}
        self.changed = False
        stored = None
        if cache_path and os.path.isfile(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as handle:
                    stored = json.load(handle)
            except (OSError, ValueError):
                stored = None
        if isinstance(stored, dict) and stored.get("version") == CACHE_VERSION and stored.get("tree") == self.tree \
                and stored.get("root") == os.path.normcase(root):
            self.entries = stored.get("files") or {}

    def direct(self, path):
        """(files, listings, read) the one file at path reaches directly, absolute paths; read are the files it only
        reads as text."""
        key = rel(self.root, path)
        if key in self.memo:
            return self.memo[key]
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        entry = self.entries.get(key)
        if not isinstance(entry, dict) or entry.get("sha") != digest:
            found, met, read = python_reach(self.root, path) if path.lower().endswith(".py") else powershell_reach(self.root, path)
            entry = {"sha": digest, "found": sorted(rel(self.root, item) for item in found),
                     "read": sorted(rel(self.root, item) for item in read),
                     "dynamic": [[rel(self.root, where), number, text] for where, number, text in met]}
            self.entries[key] = entry
            self.changed = True
        result = ([os.path.normpath(os.path.join(self.root, item)) for item in entry["found"]],
                  [(os.path.normpath(os.path.join(self.root, where)), number, text) for where, number, text in entry["dynamic"]],
                  [os.path.normpath(os.path.join(self.root, item)) for item in entry["read"]])
        self.memo[key] = result
        return result

    def save(self):
        if not self.path or not self.changed:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        staged = "{0}.{1}.tmp".format(self.path, os.getpid())
        with open(staged, "w", encoding="utf-8") as handle:
            json.dump({"version": CACHE_VERSION, "root": os.path.normcase(self.root), "tree": self.tree,
                       "files": self.entries}, handle)
        os.replace(staged, self.path)


def reach(root, harness, casing=None, analysis=None):
    """(every repo file the harness reaches, transitively, itself included; the listings met, each as
    (repo-relative file, line number, the line's text)). A folder (a pytest run) reaches every file in it and
    everything each of its scripts reaches."""
    root = os.path.normpath(os.path.abspath(root))
    start = os.path.normpath(os.path.abspath(harness))
    casing = tracked_case(root) if casing is None else casing
    analysis = Analysis(root, casing) if analysis is None else analysis
    if os.path.isdir(start):
        pending = [os.path.join(folder, name) for folder, _, names in os.walk(start) for name in names
                   if "__pycache__" not in folder]
    else:
        pending = [start]
    seen, keys, dynamic, leaves = [], set(), [], set()
    while pending:
        path = pending.pop()
        key = os.path.normcase(path)
        if key in keys:
            continue
        keys.add(key)
        seen.append(path)
        if not path.lower().endswith(SOURCE_SUFFIXES) or not os.path.isfile(path):
            continue
        found, met, read = analysis.direct(path)
        dynamic.extend(met)
        pending.extend(sorted(found))
        leaves.update(read)
    seen.extend(path for path in sorted(leaves) if os.path.normcase(path) not in keys)

    def named(path):
        return casing.get(rel(root, path).lower(), rel(root, path))

    sites = sorted({(named(where), number, text) for where, number, text in dynamic})
    return sorted({named(path) for path in seen}), sites


RE_LISTING_MARKER = re.compile(r"^#\s*GATE-LISTING:\s*(?P<body>\S.*?)\s*$")


def listing_markers(path):
    """[(needle, globs)] from the file's own GATE-LISTING lines: a listing the rules below would judge wrongly, found
    by the text in its line, and the folder globs its step then takes ("none" for one that lists only run-time state).
    The harness carries them, so no file names a harness it does not run."""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            found = RE_LISTING_MARKER.match(line)
            if not found:
                continue
            needle, _, globs = found.group("body").rpartition(" | ")
            if needle:
                rows.append((needle, () if globs.strip() == "none" else tuple(globs.split())))
    return rows


RE_TEMP_ROOT = re.compile(r"New-ClaudeTempPath|\$env:TEMP\b|GetTempPath|\btempfile\b|\bmkdtemp\b|TemporaryDirectory|"
                          r"\bscratch_dir\b|\$temp\b|\btemp_root\b", re.IGNORECASE)
RE_HARNESS_ROOT = re.compile(r"\$PSScriptRoot|__file__")
RE_LISTED_NAME = re.compile(r"\$(\w+)|\(\s*([A-Za-z_]\w*)\s*[,)]")


def rooted(pattern, text, lines, depth=3):
    """True when the listing's text, or a variable it lists, assigned in the same file within depth hops, matches
    pattern."""
    if pattern.search(text):
        return True
    if depth == 0:
        return False
    for match in RE_LISTED_NAME.finditer(text):
        name = match.group(1) or match.group(2)
        if name in ("PSScriptRoot", "true", "false", "null"):
            continue
        assigned = re.compile(r"^\s*\$?" + re.escape(name) + r"\s*=(?!=)\s*(?P<value>.+)$", re.IGNORECASE)
        for line in lines:
            found = assigned.match(line)
            if found and rooted(pattern, found.group("value"), lines, depth - 1):
                return True
    return False


def derive_inputs(root, harness, runner, analysis):
    """(the step's inputs, the warnings): every file its harness reaches, markdown included (the gate then
    fingerprints it), widened by each listing in a harness file."""
    full = os.path.join(root, harness.replace("/", os.sep))
    files, dynamic = reach(root, os.path.dirname(full) if runner == "pytest" else full, analysis.casing, analysis)
    inputs, warnings, everything = set(files), [], False
    harness_files = {name for name in files if os.path.basename(name).lower().startswith(("test_", "test-"))} | {harness}
    for where, number, text in dynamic:
        if where not in harness_files:
            continue
        rows = [needs for needle, needs in listing_markers(os.path.join(root, where.replace("/", os.sep))) if needle in text]
        if rows:
            inputs.update(rows[0])
            continue
        with open(os.path.join(root, where.replace("/", os.sep)), encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        if rooted(RE_TEMP_ROOT, text, lines):
            continue
        if rooted(RE_HARNESS_ROOT, text, lines):
            folder = os.path.dirname(where)
            inputs.add(folder + "/*" if folder else "*")
            continue
        everything = True
        warnings.append("{0}:{1} lists files the analysis cannot follow, so its step owes every edit: {2}".format(
            where, number, text[:90]))
    for where in sorted(harness_files & set(files)):
        met = [text for site, _, text in dynamic if site == where]
        for needle, _ in listing_markers(os.path.join(root, where.replace("/", os.sep))):
            if not any(needle in text for text in met):
                warnings.append("{0}: GATE-LISTING '{1}' matches no listing in the file, so it classifies nothing; "
                                "delete it".format(where, needle))
    return (["*"] if everything else sorted(inputs)), warnings


RE_STEP_MARKER = re.compile(r"^(?:#\s*)?GATE-STEP:\s*(?P<body>\S.*?)\s*$")
HEADER_LINES = 30
CONVENTION = ("Test-*.ps1", "test_*.py")


def header_marker(path):
    """The GATE-STEP marker's text from the file's first lines, else None."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for _ in range(HEADER_LINES):
            line = handle.readline()
            if not line:
                break
            found = RE_STEP_MARKER.match(line)
            if found:
                return found.group("body")
    return None


def default_name(base):
    """The step name a conventionally named file with no marker runs as: test_repos_audit.py, "Repos audit harness"."""
    stem = re.sub(r"^(test_|Test-)", "", os.path.splitext(base)[0])
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", re.sub(r"[_-]+", " ", stem)).strip().lower()
    return words[:1].upper() + words[1:] + " harness"


def discover(root, casing):
    """[{name, harness, runner, args}] for every harness in the repo, in path order."""
    steps = []
    for path in sorted(casing.values()):
        base = os.path.basename(path)
        if not base.lower().endswith((".py", ".ps1")):
            continue
        full = os.path.join(root, path.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        conventional = any(fnmatch.fnmatchcase(base, pattern) for pattern in CONVENTION)
        body = header_marker(full)
        if body is None and not conventional:
            continue
        parts = [part.strip() for part in (body or default_name(base)).split("|")]
        if parts[0].lower() == "none":
            continue
        runner, args = "file", []
        for part in parts[1:]:
            if part == "pytest":
                runner = "pytest"
            elif part.startswith("args:"):
                args = part[len("args:"):].split()
        steps.append({"name": parts[0], "harness": path, "runner": runner, "args": args})
    return steps


def step_command(root, step):
    full = os.path.join(root, step["harness"].replace("/", os.sep))
    if step["runner"] == "pytest":
        return ["python", "-m", "pytest", "-x", "-q", os.path.dirname(full)]
    if full.lower().endswith(".py"):
        return ["python", full] + step["args"]
    return ["pwsh", "-File", full] + step["args"]


def default_cache_path(root):
    """The plan cache for one checkout, in the account's own local state (claude_temp.account_root), never the share:
    it holds paths of this machine's checkout."""
    hooks = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import claude_temp
    key = hashlib.sha256(os.path.normcase(os.path.normpath(os.path.abspath(root))).encode("utf-8")).hexdigest()[:16]
    return os.path.join(claude_temp.account_root(), "step-inputs", key + ".json")


class PlanError(Exception):
    pass


def plan(root, cache_path=None):
    """{"steps": [{name, harness, command, inputs}], "warnings": [...]} for the repo. Two harnesses claiming one step
    name is the one error, since a step's record is kept by name."""
    root = os.path.normpath(os.path.abspath(root))
    casing = tracked_case(root)
    analysis = Analysis(root, casing, cache_path)
    steps = discover(root, casing)
    names = {}
    for step in steps:
        names.setdefault(step["name"], []).append(step["harness"])
    clashes = {name: paths for name, paths in names.items() if len(paths) > 1}
    if clashes:
        raise PlanError("; ".join("{0} is claimed by {1}".format(name, ", ".join(paths)) for name, paths in sorted(clashes.items())))
    out, warnings = [], []
    for step in steps:
        inputs, found = derive_inputs(root, step["harness"], step["runner"], analysis)
        warnings.extend(found)
        out.append({"name": step["name"], "harness": step["harness"], "command": step_command(root, step), "inputs": inputs})
    analysis.save()
    return {"steps": out, "warnings": warnings}


def main(argv):
    if len(argv) < 3:
        sys.stdout.write(__doc__)
        return 2
    root = argv[1]
    if argv[2] == "--plan":
        cache = argv[argv.index("--cache") + 1] if "--cache" in argv else default_cache_path(root)
        try:
            result = plan(root, cache)
        except PlanError as error:
            sys.stderr.write("step plan refused: {0}\n".format(error))
            return 1
        sys.stdout.write(json.dumps(result))
        return 0
    casing = tracked_case(os.path.abspath(root))
    for harness in argv[2:]:
        files, dynamic = reach(root, harness, casing)
        for name in files:
            print(name)
        for where, number, text in dynamic:
            print("DYNAMIC: {0}:{1} {2}".format(where, number, text[:100]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
