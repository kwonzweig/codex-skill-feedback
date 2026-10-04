"""Read this turn's desktop transcript and emit one native Hook stats message."""
import json
import mmap
import posixpath
import re
import sys
from urllib.parse import unquote, urlsplit


def path_key(value, cwd=""):
    if value.startswith("file:"):
        uri = urlsplit(value)
        value = unquote(uri.path)
        if uri.netloc:
            value = "//" + uri.netloc + value
        elif re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    value = value.replace("\\", "/")
    if not value.startswith("/") and not re.match(r"^[A-Za-z]:/", value):
        value = posixpath.join(path_key(cwd), value) if cwd else value
    value = posixpath.normpath(value)
    return value.casefold() if re.match(r"^[A-Za-z]:/|^//", value) else value


def catalog_text(message):
    if message.get("type") != "message" or message.get("role") != "developer":
        return None
    text = "\n".join(part.get("text", "") for part in message.get("content", []))
    block = re.search(r"<skills_instructions>(.*?)</skills_instructions>", text, re.S)
    return block[1] if block else None


def catalog(message):
    text = catalog_text(message)
    if text is None:
        return None
    if "### Available skills" not in text:
        raise ValueError("Unknown catalog format")
    roots = dict(re.findall(r"^- `([^`]+)` = `([^`]+)`\s*$", text, re.M))
    result = {}
    available = text.split("### Available skills", 1)[1].split("\n##", 1)[0]
    for line in available.splitlines():
        if not line.strip():
            continue
        entry = re.fullmatch(r"- (.+?): .*?\(file: (.+[/\\]SKILL\.md)\)\s*", line)
        if not entry:
            raise ValueError("Unknown catalog entry")
        name, path = entry.groups()
        alias, _, suffix = path.partition("/")
        if alias in roots:
            path = roots[alias] + "/" + suffix
        elif re.match(r"^r\d+$", alias):
            raise ValueError("Unknown skill root")
        result.setdefault(posixpath.dirname(path_key(path)), name)
    return result


def records_backwards(transcript):
    """Read from a fixed snapshot of the file's end, without loading its history."""
    with open(transcript, "rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
            end, tail = len(data), True
            while end > 0:
                start = data.rfind(b"\n", 0, end) + 1
                line, end = data[start:end], start - 1
                if not line.strip():
                    tail = False
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if not tail:
                        raise
                tail = False


def messages(record):
    if record.get("type") == "response_item":
        return [record.get("payload", {})]
    if record.get("type") == "compacted":
        return record.get("payload", {}).get("replacement_history", [])
    return []


def starts_turn(record):
    return record.get("type") == "turn_context" or (
        record.get("type") == "event_msg" and record.get("payload", {}).get("type") == "task_started")


def collect(transcript, turn_id, session_id):
    if any(not isinstance(value, str) or not value.strip() for value in (turn_id, session_id)):
        raise ValueError("Missing turn or session identity")
    # Keep this turn and its preceding catalog, then replay only that small window.
    window, found_start = [], False
    for record in records_backwards(transcript):
        payload = record.get("payload", {})
        # turn_context can repeat after compaction inside the same turn.
        if (record.get("type") == "event_msg" and payload.get("type") == "task_started"
                and payload.get("turn_id") == turn_id):
            found_start = True
        if record.get("type") in ("event_msg", "turn_context", "response_item", "compacted"):
            window.append(record)
        if found_start and any(catalog_text(message) is not None for message in messages(record)):
            break

    skills, used, found_turn = None, set(), False
    for record in reversed(window):
        kind, payload = record.get("type"), record.get("payload", {})
        if starts_turn(record):
            if found_turn and payload.get("turn_id") != turn_id:
                break
            found_turn |= payload.get("turn_id") == turn_id
        for message in messages(record):
            try:
                updated = catalog(message)
            except ValueError:
                # An obsolete format is harmless if replaced before a read.
                skills = None
                continue
            if updated is not None:
                skills = updated
        if (kind != "event_msg" or payload.get("type") != "item_completed"
                or payload.get("turn_id") != turn_id or payload.get("thread_id") != session_id):
            continue
        item = payload.get("item", {})
        if (item.get("type") != "CommandExecution" or item.get("status") != "completed"
                or item.get("exit_code") != 0 or item.get("stderr")
                or not (item.get("aggregated_output") or item.get("stdout") or "").strip()):
            continue
        for command in item.get("parsed_cmd", []):
            if command.get("type") != "read" or not command.get("path"):
                continue
            if skills is None:
                raise ValueError("Read without a supported catalog")
            path = path_key(command["path"], item.get("cwd", ""))
            for root in sorted(skills, key=len, reverse=True):
                if path.startswith(root + "/"):
                    used.add(skills[root])
                    break
    if not found_turn or skills is None:
        raise ValueError("Turn or catalog unavailable")
    return set(skills.values()), used


def section(title, names, empty):
    return f"{title} ({len(names)})\n" + "\n".join("- " + name for name in sorted(names, key=str.casefold) or [empty])


def handle(event):
    if event.get("hook_event_name") != "Stop":
        return {}
    try:
        accessible, used = collect(event["transcript_path"], event["turn_id"], event["session_id"])
        message = section("Available skills", accessible, "None") + "\n\n" + section(
            "Read this turn", used, "None")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        message = "Skill data unavailable. Transcript missing or unsupported."
    return {"systemMessage": message}


if __name__ == "__main__":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        result = handle(json.load(sys.stdin))
    except (ValueError, TypeError, AttributeError):
        result = {}
    print(json.dumps(result))
