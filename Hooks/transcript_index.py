"""A transcript's candidate lines, found once and resumed from where the last call stopped.

The hooks read the session transcript on every call to learn what the operator said, which goal is pinned and
what the session declared. Read whole, a long session's transcript costs seconds per reader, and a gate launch
runs several readers. Here a reader asks for the lines holding its byte needles (candidate_lines), or for the
tool results answering the tool calls it found (result_lines). The byte offsets of the matching lines are kept per
transcript and kind, with the offset scanned to, so a call reads only the bytes appended since.

The index holds offsets, never content. Every line is read back from the transcript itself, from an offset that
must start a line, and must still match, so a tampered index can hide lines but never invent them. A transcript
whose first bytes or the bytes just before the scanned offset changed is a rewritten file (a synced copy replaced
it) and is scanned again from the start. Only complete lines are indexed, so a line still being written is read
whole on the next call.
"""

import hashlib
import json
import os
import re
import tempfile

INDEX_VERSION = 2
CHECK_BYTES = 4096
CHUNK_BYTES = 8 * 1024 * 1024
INDEX_DIRECTORY = "transcript-index"
RESULTS_KIND = "tool results"
RE_RESULT_ID = re.compile(rb'"tool_use_id": ?"([^"\\]+)"')
scanned_bytes = {"last": 0}
cache_root_override = {"root": None}


def default_cache_root():
    """<claude temp root>\\transcript-index, beside every other hook's state; a harness points it at a fixture."""
    if cache_root_override["root"]:
        return cache_root_override["root"]
    import claude_temp
    return os.path.join(claude_temp.claude_root(), INDEX_DIRECTORY)


def index_file(cache_root, path, kind):
    key = os.path.normcase(os.path.abspath(path)) + "\0" + kind
    return os.path.join(cache_root, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def needles_kind(groups):
    return "needles\0" + "\0".join("\1".join(part.decode("latin-1") for part in group) for group in groups)


def as_groups(needles):
    """Each needle as a tuple of byte strings that must all appear: a bare byte string is a group of one."""
    return tuple((needle,) if isinstance(needle, bytes) else tuple(needle) for needle in needles)


def matches(line, groups):
    return any(all(part in line for part in group) for group in groups)


def result_ids(line):
    return sorted({found.decode("utf-8", errors="replace") for found in RE_RESULT_ID.findall(line)})


def fingerprint(handle, start, length):
    if start < 0 or length <= 0:
        return ""
    handle.seek(start)
    return hashlib.sha1(handle.read(length)).hexdigest()


def load_index(file_name):
    try:
        with open(file_name, "r", encoding="utf-8") as handle:
            index = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(index, dict) or index.get("version") != INDEX_VERSION \
            or not isinstance(index.get("offset"), int) or not isinstance(index.get("lines"), list):
        return None
    return index


def save_index(file_name, index):
    """Written to a temporary file and renamed over the index, so a concurrent reader sees one whole index."""
    try:
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(file_name), suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(index, handle, separators=(",", ":"))
        os.replace(temporary, file_name)
    except OSError:
        return


def still_valid(index, handle, size):
    offset = index["offset"]
    if offset > size:
        return False
    if index.get("head") != fingerprint(handle, 0, min(CHECK_BYTES, size)):
        return False
    tail_start = max(0, offset - CHECK_BYTES)
    return index.get("tail") == fingerprint(handle, tail_start, offset - tail_start)


def scan(handle, start, select):
    """(entries for the complete lines from `start` that `select(line, offset)` keeps, the offset after the last
    complete line), read a chunk at a time."""
    handle.seek(start)
    entries, position, carry = [], start, b""
    while True:
        chunk = handle.read(CHUNK_BYTES)
        if not chunk:
            return entries, position
        data = carry + chunk
        end = data.rfind(b"\n") + 1
        cursor = 0
        while cursor < end:
            line_end = data.index(b"\n", cursor) + 1
            entry = select(data[cursor:line_end], position + cursor)
            if entry is not None:
                entries.append(entry)
            cursor = line_end
        position += end
        carry = data[end:]


def read_line(handle, offset, limit):
    """The complete line starting at `offset`, else None for an offset that does not start a line below `limit`."""
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset >= limit:
        return None
    if offset:
        handle.seek(offset - 1)
        if handle.read(1) != b"\n":
            return None
    handle.seek(offset)
    line = handle.readline()
    return line if line.endswith(b"\n") else None


def indexed(path, cache_root, kind, select):
    """(open handle, entries, scanned offset) for the transcript at `path`, resumed from and saved to cache_root;
    None for a missing transcript. The caller closes the handle."""
    try:
        handle = open(path, "rb")
    except (OSError, TypeError):
        return None
    size = os.fstat(handle.fileno()).st_size
    file_name = index_file(cache_root, path, kind) if cache_root else None
    index = load_index(file_name) if file_name else None
    if index is None or not still_valid(index, handle, size):
        index = {"version": INDEX_VERSION, "offset": 0, "lines": []}
    found, reached = scan(handle, index["offset"], select)
    scanned_bytes["last"] = max(0, size - index["offset"])
    if reached != index["offset"]:
        index["lines"] = index["lines"] + found
        index["offset"] = reached
        index["head"] = fingerprint(handle, 0, min(CHECK_BYTES, os.fstat(handle.fileno()).st_size))
        tail_start = max(0, reached - CHECK_BYTES)
        index["tail"] = fingerprint(handle, tail_start, reached - tail_start)
        if file_name:
            save_index(file_name, index)
    return handle, index["lines"], index["offset"]


def candidate_lines(path, needles, cache_root=None):
    """The raw bytes of every complete line of the transcript at `path` that holds any of `needles`, in file order;
    [] for a missing transcript. A needle is a byte string, or a tuple of byte strings a line must hold all of.
    With cache_root, the offsets are resumed and saved there."""
    groups = as_groups(needles)
    opened = indexed(path, cache_root, needles_kind(groups),
                     lambda line, offset: offset if matches(line, groups) else None)
    if opened is None:
        return []
    handle, entries, limit = opened
    with handle:
        lines = []
        for offset in entries:
            line = read_line(handle, offset, limit)
            if line is not None and matches(line, groups):
                lines.append(line)
        return lines


def result_lines(path, ids, cache_root=None):
    """The raw bytes of every complete line of the transcript at `path` carrying a tool result for any of `ids`, in
    file order; [] for a missing transcript. Every result line is indexed by the tool_use_ids it holds, so a reader
    finds the results for the calls it found whatever the result text says."""
    wanted = {str(tool_id) for tool_id in ids if tool_id}
    if not wanted:
        return []

    def select(line, offset):
        found = result_ids(line)
        return [offset, found] if found else None

    opened = indexed(path, cache_root, RESULTS_KIND, select)
    if opened is None:
        return []
    handle, entries, limit = opened
    with handle:
        lines = []
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[1], list) \
                    or not wanted.intersection(str(tool_id) for tool_id in entry[1]):
                continue
            line = read_line(handle, entry[0], limit)
            if line is not None and wanted.intersection(result_ids(line)):
                lines.append(line)
        return lines


def parsed(lines):
    """Each line parsed as JSON, skipping a line that does not parse or is not an object."""
    records = []
    for raw in lines:
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def candidate_records(path, needles, cache_root=None):
    return parsed(candidate_lines(path, needles, cache_root))


def result_records(path, ids, cache_root=None):
    return parsed(result_lines(path, ids, cache_root))
