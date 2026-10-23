#!/usr/bin/env python3

import argparse
import contextlib
import difflib
import gzip
import http.client
import json
import os
import re
import shutil
import signal
import sqlite3
import string
import subprocess
import sys
import tempfile
import time


BACKENDS = {"ollama": ("Ollama; --host selects the server", "/v1/chat/completions", "deepseek-v4.1-flash:cloud"),
            "meta": ("Meta, the default backend", "https://api.meta.ai/v1/responses", "muse-spark-1.3-contributor"),
            "anthropic": ("Anthropic", "https://api.anthropic.com/v1/messages", "claude-haiku-5-5"),
            "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1/messages", "openrouter/auto")}
OLLAMA_HOST, TIMEOUT = "http://127.0.0.1:11434", 180

WINDOWS, UNAME = os.name == "nt", os.uname() if hasattr(os, "uname") else None
OS_DESCRIPTION = ("%s %s (%s)" % (UNAME.sysname, UNAME.release, UNAME.machine) if UNAME
                  else "Windows (%s)" % os.environ.get("PROCESSOR_ARCHITECTURE", "unknown"))
SHELL, SHELL_PATH = ("shell", os.environ.get("COMSPEC", "cmd.exe")) if WINDOWS else ("bash", shutil.which("bash") or "/bin/bash")
STOP, TOUCH = ("taskkill /F /T /PID %s", "type nul > prog.py") if WINDOWS else ("kill -- -%s", "touch prog.py")
SLEEP = '"%s" -c "import time; time.sleep(5)"' % sys.executable if WINDOWS else "sleep 5"
CHUNK_SIZE, TRACKED_FILES = 65536, 50
EX_OK, EX_USAGE, EX_SOFTWARE, EX_CONFIG, INTERRUPTED = 0, 64, 70, 78, 130

FILE_ARG = {"type": "string", "description": "Path to the file, absolute or relative to the working directory."}
CONTENT_ARG = {"type": "string", "description": "Literal replacement or inserted content."}
ID_ARG = {"type": "string", "description": "Line id copied from read with ids=true, such as '7qx2'; an inclusive range is "
          "'7qx2..m04c'. An id names its line until that line is replaced or deleted: edits elsewhere never change it, "
          "so keep using ids from the latest tool output without re-reading."}
EDIT_NOTE = (" Works only on an existing file read with ids=true in this session (ids are kept for the %s most recently "
             "used files); otherwise the call is denied. Content is literal, so include exact indentation. After each "
             "edit use the fresh ids it returns; if an id is unknown, read with ids=true again." % TRACKED_FILES)


TOOLS = [{"type": "function", "name": name, "description": description,
          "parameters": {"type": "object", "properties": properties, "required": required}}
         for name, description, properties, required in [
    (SHELL, "Run one command line in %s, in the working directory, and return its output (stdout and stderr) and exit "
            "code. `cd` does not persist between calls: chain with `&&`, or pass cwd. To create a file, make it empty "
            "(%s), then set its whole content with one replace using id 'start'." % (SHELL_PATH, TOUCH), {
        "cmd": {"type": "string", "description": "The command line to run."},
        "timeout": {"type": "integer", "description": "Seconds before the command is killed (default %s). "
                    "Background commands have no limit." % TIMEOUT},
        "cwd": {"type": "string", "description": "Directory to run in; it stays the working directory for all later calls "
                "and relative file paths."},
        "background": {"type": "boolean", "description": "Start the command detached, in its own process group, and return "
                       "at once with its pid and output file. It runs until it exits or you stop it (%s); ucoder kills it "
                       "on exit. The first %s result after it exits reports its exit code; to wait, run %s, and read its "
                       "output file with read." % (STOP % "<pid>", SHELL, SLEEP)},
    }, ["cmd"]),
    ("read", "Read a file. By default the result is its plain content; with ids=true each row is '<id>|<content>' and "
             "those ids are what replace, insert and delete take: copy only the id, for example '7qx2' from "
             "'7qx2|    return value'. If the file is large the result is truncated; page through it with 'offset' and "
             "'limit'. Example: {\"file_path\": \"prog.py\", \"ids\": true}", {
        "file_path": {"type": "string", "description": (
            "The path to the file to read, absolute or relative to the working directory.")},
        "offset": {"type": "integer", "description": (
            "Optional: the 0-based line number to start reading from. Use for paginating through large files.")},
        "limit": {"type": "integer", "description": "Optional: maximum number of lines to read. Use with 'offset' to "
                  "paginate through large files. If omitted, reads the entire file (up to a default limit)."},
        "ids": {"type": "boolean", "description": "Optional, default false: show each line as '<id>|<content>' and keep "
                "the ids for edits. Set it before replace, insert or delete; leave it off to just read."},
    }, ["file_path"]),
    ("replace", "Replace one line or range by id. Prefer this over delete+insert: editing in place keeps surrounding ids "
                "stable." + EDIT_NOTE + " Examples: {\"file\": \"prog.py\", \"id\": \"7qx2\", \"content\": "
                "\"    return 2\"}, {\"file\": \"prog.py\", \"id\": \"7qx2..m04c\", \"content\": \"replacement\\nfor those "
                "lines\"}", {"file": FILE_ARG, "id": ID_ARG, "content": CONTENT_ARG}, ["file", "id", "content"]),
    ("insert", "Insert before or after one line id, never a range: to insert between two lines, use the first line's id "
               "with position=after or the second line's with position=before." + EDIT_NOTE + " Example: "
               "{\"file\": \"prog.py\", \"id\": \"7qx2\", \"content\": \"new line\", \"position\": \"after\"}", {
        "file": FILE_ARG,
        "id": {"type": "string", "description": "Single line id copied from read with ids=true, such as '7qx2'. "
               "Use position before/after for placement."},
        "content": CONTENT_ARG,
        "position": {"type": "string", "enum": ["after", "before"],
                     "description": "Placement relative to the id. Defaults to after."},
    }, ["file", "id", "content"]),
    ("delete", "Delete one line or range by id. Use this only to remove code outright; to change a line, use replace "
               "instead of delete+insert." + EDIT_NOTE + " Example: {\"file\": \"prog.py\", \"id\": \"7qx2..m04c\"}",
     {"file": FILE_ARG, "id": ID_ARG}, ["file", "id"]),
    ("memory", "The memory tool: use sparingly to keep crucial project decisions in memory, referenced at the beginning "
               "of a session to retrieve useful information, and written to, when useful, at the end of a task. Memories "
               "store important routines (actions that could be repeated), project specifications, and user preferences. "
               "The names listed under Memories in your system prompt are all there are; read one with "
               "{\"read\": \"how-to-compile.md\"}. To write or overwrite: "
               "{\"write\": \"verification-routine-before-run.md\", \"summary\": \"a brief summary of the routine\", "
               "\"content\": \"long text\"}.", {
        "read": {"type": "string", "description": "Name of the memory to read, as listed in the system prompt."},
        "write": {"type": "string", "description": "Name of the memory to write or overwrite. It is normalized to "
                  "lowercase words joined by hyphens plus the .md extension, so 'How To Compile' and 'how-to-compile.md' "
                  "are the same memory; the reply names the stored memory."},
        "summary": {"type": "string", "description": "Brief summary of the memory (required when writing)."},
        "content": {"type": "string", "description": "Full content of the memory (required when writing)."},
        "title": {"type": "string", "description": "Optional title; defaults to the memory name."},
    }, []),
]]


def guard(fn):
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as err:
            terminal.warn("%s failed: %s" % (fn.__qualname__, terminal.explain(err)))

    return call


def interruptible(fn):
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (KeyboardInterrupt, EOFError):
            terminal.write("\n")
            return INTERRUPTED

    return call


class Terminal:
    RESET, BOLD, BLUE, YELLOW, CLEAR = "\033[0m", "\033[1m", "\033[34m", "\033[33m", "\033[H\033[2J"
    RED, MAGENTA, ORANGE, GREY = "\033[31m", "\033[35m", "\033[38;5;208m", "\033[38;5;244m"
    MASCOT = (" █▜██▜", " ▜███▛", "▝▝▝ ▝▝")
    FILE_TOOLS = ("read", "replace", "insert", "delete")
    STREAM_IDLE, STREAM_THINKING, STREAM_TEXT, STREAM_BREAK = 0, 1, 2, 3
    CALL_CHARS, ECHO_CHARS, RESULT_CHARS, PATH_CHARS = 500, 500, 300, 40

    def __init__(self):
        self.stream_state = self.STREAM_IDLE
        self.bold, self.word, self.column = False, "", 2
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")

    def write(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def paint(self, text, *codes):
        return "".join(codes) + text + self.RESET

    def line(self, text, *codes):
        self.write(self.paint(text, *codes) + "\n")

    def warn(self, text):
        self.line("! " + text, self.YELLOW)

    def error(self, err):
        self.line("⏺ " + self.explain(err), self.RED)

    def explain(self, err):
        if type(err) in (str, Exception):
            return str(err)
        return "%s: %s" % (type(err).__name__, err)

    def clip(self, text, limit, suffix="…"):
        if len(text) <= limit:
            return text
        return text[:limit - len(suffix)].rstrip("\n") + suffix

    def clip_path(self, path):
        return path if len(path) <= self.PATH_CHARS else path[:self.PATH_CHARS // 2] + "…" + path[1 - self.PATH_CHARS // 2:]

    def separator(self):
        self.line("─" * shutil.get_terminal_size((80, 24)).columns, self.GREY)

    def banner(self, model, cwd):
        self.write(self.CLEAR + "\n")
        rows = zip(self.MASCOT, (self.paint("ucoder ", self.BOLD), "", ""), (OS_DESCRIPTION, model, self.clip_path(cwd)))
        for art, title, text in rows:
            self.write(self.paint(art, self.BLUE) + "   " + title + self.paint(text, self.GREY) + "\n")

    def stream(self, event):
        kind, item = event.get("type"), (event.get("item") or {}).get("type")
        if kind == "response.output_text.delta":
            self.stream_to(self.STREAM_TEXT, "\n" + self.paint("⏺ "))
            self.write(self.emphasis(self.wrap(str(event.get("delta", "")))))
        elif kind == "response.output_item.added" and item == "reasoning":
            self.stream_to(self.STREAM_THINKING, "\n" + self.paint("⏺ thinking", self.MAGENTA))
        elif kind in ("response.output_item.added", "response.output_item.done") and item == "message":
            if self.stream_state == self.STREAM_TEXT:
                self.stream_state = self.STREAM_BREAK

    def emphasis(self, text):
        out = ""
        for i, part in enumerate(text.split("**")):
            self.bold ^= i > 0
            out += self.paint(part, self.BOLD, self.BLUE) if self.bold else part
        return out

    def wrap(self, text, final=False):
        tokens = re.findall(r"\n|[^\S\n]+|\S+", self.word + text)
        self.word = "" if final or not tokens or tokens[-1].isspace() else tokens.pop()
        out, columns = "", shutil.get_terminal_size((80, 24)).columns
        for token in tokens:
            width = len(token.replace("**", ""))
            if token == "\n" or self.column > 2 and self.column + width > columns:
                out, self.column = out + "\n  ", 2
                if token.isspace():
                    continue
            out, self.column = out + token, self.column + width
        return out

    def stream_to(self, state, text):
        if self.stream_state != state:
            self.write(self.emphasis(self.wrap("", True)) + text)
            self.stream_state, self.bold, self.column = state, False, 2

    def tool_call(self, name, args):
        colour = self.ORANGE if name in self.FILE_TOOLS else self.BLUE
        if name == "memory":
            name, colour = "memorize" if args.get("write") else "remember", self.YELLOW
            args = {"file_path": args.get("write") or args.get("read") or ""}
        elif name == "read" and args.flag("ids"):
            name = "inspect"
        summary = self.clip(self._gist(args).replace("\n", " "), self.CALL_CHARS)
        title = self.paint("⏺ " + name[:1].upper() + name[1:], colour)
        self.write("\n" + title + "(" + self.paint(summary, self.GREY) + ")\n")

    def _gist(self, args):
        for key in ("cmd", "command", "file_path", "file"):
            if key in args:
                text = self.clip_path(str(args[key])) if key in ("file_path", "file") else str(args[key])
                return text + (" %s" % args["id"] if "id" in args else "")
        return ", ".join(["%s=%r" % (k, v) for k, v in list(args.items())[:2]])

    def tool_result(self, result):
        lines = result.split("\n")
        more = " … +%s lines" % (len(lines) - 1) if len(lines) > 1 else ""
        colour = self.RED if result.startswith("error:") else self.GREY
        self.line("  └  " + self.clip(lines[0], self.RESULT_CHARS, "" if more else "…") + more, colour)

    def tool_output(self, line, *codes):
        self.write(self.paint("  │  ", self.GREY) + self.paint(self.clip(line, self.ECHO_CHARS), *codes) + "\n")


terminal = Terminal()


class Store:
    HISTORY_LIMIT, SUMMARY_CHARS = 1000, 160
    EDITOR, COMPOSE_KEY, NEWLINE, ERASE_ABOVE = "vim", r'"\C-g": "\C-e\t\n"', "↵", "\033[A\r\033[K"

    def __init__(self, cwd):
        os.makedirs(os.path.join(cwd, ".ucoder"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(cwd, ".ucoder", "ucoder.db"), isolation_level=None)
        self.db.executescript("CREATE TABLE IF NOT EXISTS memory "
                              "(name TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL, content BLOB NOT NULL);"
                              "CREATE TABLE IF NOT EXISTS history (line TEXT);")
        self.last = (self.db.execute("SELECT line FROM history ORDER BY rowid DESC").fetchone() or (None,))[0]
        self.readline = self._import_readline()
        for name, content in self.db.execute("SELECT name, content FROM memory WHERE typeof(content) = 'text'").fetchall():
            self.db.execute("UPDATE memory SET content = ? WHERE name = ?", (self._deflate(content), name))

    def recall(self, name):
        row = self.db.execute("SELECT title, summary, content FROM memory WHERE name = ?", (self._key(name),)).fetchone()
        if row is None:
            raise Exception("no memory named '%s'. Stored: %s" % (name, self._stored() or "(none)"))
        return "\n".join(row[:2] + (self._inflate(row[2]),))

    def memorize(self, name, title, summary, content):
        key = self._key(name)
        self.db.execute("INSERT INTO memory VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET title = excluded.title, "
                        "summary = excluded.summary, content = excluded.content",
                        (key, title, summary, self._deflate(content)))
        return key

    def listing(self):
        rows = self.db.execute("SELECT name, summary FROM memory ORDER BY rowid DESC").fetchall()
        return [(name, terminal.clip(summary, self.SUMMARY_CHARS)) for name, summary in rows]

    def _deflate(self, content):
        return gzip.compress(content.encode(), 9, mtime=0)

    def _inflate(self, content):
        if isinstance(content, str):
            return content
        with contextlib.suppress(OSError, EOFError):
            return gzip.decompress(content).decode()
        return content.decode("utf-8", "replace")

    def _key(self, name):
        words = re.split("[^a-z0-9]+", name.lower().removesuffix(".md").removesuffix(".markdown"))
        return "-".join([word for word in words if word]) + ".md"

    def _stored(self):
        return ", ".join([name for (name,) in self.db.execute("SELECT name FROM memory ORDER BY name")])

    @interruptible
    def ask(self):
        terminal.separator()
        prompt = terminal.paint("❯ ", terminal.BOLD, terminal.BLUE)
        if self.readline is not None:
            prompt = re.sub("(\x1b\\[[0-9;]*[A-Za-z])", "\x01\\1\x02", prompt)
        line = input(prompt)
        while line.endswith("\t"):
            line = self._compose(line[:-1].replace(self.NEWLINE, "\n"))
            if not line:
                terminal.write(self.ERASE_ABOVE)
                line = input(prompt)
        line = line.replace("\n", self.NEWLINE)
        if line.strip() and line != self.last:
            self.last = line
            self._remember(line)
        terminal.separator()
        return line.replace(self.NEWLINE, "\n").strip()

    def _compose(self, draft):
        with tempfile.NamedTemporaryFile("w", prefix="ucoder-input-", suffix=".md", delete_on_close=False) as handle:
            handle.write(draft)
            handle.close()
            if subprocess.call('%s "%s"' % (os.environ.get("EDITOR") or self.EDITOR, handle.name), shell=True):
                return ""
            with open(handle.name) as edited:
                return edited.read().strip()

    @guard
    def _remember(self, line):
        if self.readline is not None:
            self.readline.add_history(line)
        self.db.execute("INSERT INTO history VALUES (?)", (line,))
        self.db.execute("DELETE FROM history WHERE rowid <= (SELECT MAX(rowid) FROM history) - ?", (self.HISTORY_LIMIT,))

    def _import_readline(self):
        with contextlib.suppress(ImportError):
            import readline
            self._fill_readline(readline)
            return readline

    @guard
    def _fill_readline(self, readline):
        readline.parse_and_bind(self.COMPOSE_KEY)
        readline.parse_and_bind("set enable-bracketed-paste on")
        readline.parse_and_bind("set horizontal-scroll-mode off")
        readline.set_auto_history(False)
        readline.set_history_length(self.HISTORY_LIMIT)
        readline.clear_history()
        for (line,) in self.db.execute("SELECT line FROM history ORDER BY rowid"):
            readline.add_history(line.replace("\n", self.NEWLINE))


class JsonObject(dict):
    @staticmethod
    def parse(text):
        with contextlib.suppress(ValueError):
            value = json.loads(text)
            return JsonObject(value) if isinstance(value, dict) else None

    def flag(self, key):
        value = self.get(key)
        return value is True or str(value).strip().lower() == "true"

    def positive_int(self, key, default=0):
        value = self.get(key)
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, (int, float)) and value >= 1:
            return int(value)
        return default

    def text(self, key):
        value = self.get(key)
        return value.strip() if isinstance(value, str) else ""

    def required(self, key, why=""):
        if not self.text(key):
            raise Exception("'%s' is required%s" % (key, why))
        return self[key]


class Shell:
    MAX_RUN_OUTPUT = 256 * 1048576
    OVERFLOW = "its output passed %s MB" % (MAX_RUN_OUTPUT // 1048576)

    def __init__(self, cwd):
        self.cwd = os.path.abspath(cwd)
        self.spawned = {}
        self.logs = tempfile.TemporaryDirectory(prefix="ucoder-", ignore_cleanup_errors=True)

    def run(self, args):
        cmd = args.text("cmd") or args.text("command") or args.required("cmd")
        if args.text("cwd"):
            path = os.path.abspath(os.path.join(self.cwd, os.path.expanduser(args.text("cwd"))))
            if not os.path.isdir(path):
                raise Exception("cwd is not a directory: %s" % path)
            self.cwd = path
        finished = self._reap()
        if args.flag("background"):
            result, self.status = self._spawn(cmd), "Running in the background"
        else:
            result = self._foreground(cmd, args.positive_int("timeout", TIMEOUT))
        return "\n\n".join([result] + finished + self._reap())

    def close(self):
        for proc, _log in self.spawned.values():
            self._kill(proc)
        self.logs.cleanup()

    def _foreground(self, cmd, timeout):
        proc, log = self._start(cmd, "run-")
        start = time.monotonic()
        killed = ""
        try:
            with open(log, "rb") as handle:
                output = CommandOutput(handle)
                while proc.poll() is None and not killed:
                    output.echo()
                    if time.monotonic() - start >= timeout:
                        killed = "killed after %ss" % timeout
                    elif os.path.getsize(log) > self.MAX_RUN_OUTPUT:
                        killed = "killed: " + self.OVERFLOW
                    else:
                        time.sleep(0.05)
                self._kill(proc)
                status = killed or "exit code %s · %.1fs" % (proc.returncode, time.monotonic() - start)
                self.status = terminal.paint(status, terminal.RED if proc.returncode else terminal.GREY)
                text = output.text()
        finally:
            self._kill(proc)
            self._remove(log)
        if not killed and proc.returncode == 0:
            return text or "(no output; exit 0)"
        return ("%s\n(%s)" % (text, killed or "exit code %s" % proc.returncode)).strip()

    def _start(self, cmd, prefix):
        fd, log = tempfile.mkstemp(prefix=prefix, suffix=".log", dir=self.logs.name)
        options = {"shell": True, "cwd": self.cwd, "stdin": subprocess.DEVNULL, "stdout": fd, "stderr": subprocess.STDOUT}
        options.update({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
                       else {"executable": SHELL_PATH, "start_new_session": True})
        try:
            proc = subprocess.Popen(cmd, **options)
        finally:
            os.close(fd)
        return proc, log

    def _spawn(self, cmd):
        proc, log = self._start(cmd, "bg-")
        self.spawned[proc.pid] = (proc, log)
        return "started in the background: pid %s, output in %s (stop it with: %s)" % (proc.pid, log, STOP % proc.pid)

    def _reap(self):
        lines = []
        for pid in list(self.spawned):
            proc, log = self.spawned[pid]
            overflow = proc.poll() is None and os.path.exists(log) and os.path.getsize(log) > self.MAX_RUN_OUTPUT
            if overflow:
                self._kill(proc)
            code = proc.poll()
            if code is None:
                continue
            del self.spawned[pid]
            if overflow:
                status = "was killed: " + self.OVERFLOW
            elif code < 0:
                status = "was killed by signal %s" % -code
            else:
                status = "exited with code %s" % code
            lines.append("background pid %s %s; output in %s" % (pid, status, log))
        return lines

    @guard
    def _kill(self, proc):
        if proc.poll() is not None:
            return
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

    @guard
    def _remove(self, path):
        os.unlink(path)


class CommandOutput:
    MAX_OUTPUT, ECHO_LINES = 30_000, 6

    def __init__(self, handle):
        self.handle = handle
        self.echoed = 0

    @guard
    def echo(self, final=False):
        while self.echoed < self.ECHO_LINES:
            line = self.handle.readline()
            if not line or not final and not line.endswith(b"\n"):
                return self.handle.seek(-len(line), os.SEEK_CUR)
            self.echoed = self.echoed + 1
            terminal.tool_output(line.decode("utf-8", "replace").rstrip("\r\n"))

    def text(self):
        self.echo(True)
        self.handle.seek(0)
        head, tail, total, rows, half = "", "", 0, 0, self.MAX_OUTPUT // 2
        for data in iter(lambda: self.handle.read(CHUNK_SIZE) + self.handle.readline(), b""):
            chunk = data.decode("utf-8", "replace")
            head, tail, total = (head + chunk)[:self.MAX_OUTPUT], (tail + chunk)[-half:], total + len(chunk)
            rows = rows + data.count(b"\n") + (not data.endswith(b"\n"))
        if rows > self.echoed:
            terminal.tool_output("… +%s lines" % (rows - self.echoed), terminal.GREY)
        if total <= self.MAX_OUTPUT:
            return head.strip()
        return ("%s\n... (%s characters omitted) ...\n%s" % (head[:half], total - self.MAX_OUTPUT, tail)).strip()


class TextFile:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as handle:
            data = b"".join(iter(lambda: handle.read(CHUNK_SIZE), b""))
        if b"\0" in data[:8000]:
            raise Exception("binary file is not supported: %s" % path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Exception("invalid UTF-8 in %s" % path)
        del data
        crlf = text.count("\r\n")
        if crlf and text.count("\n") > crlf:
            raise Exception("mixed newlines are not supported: %s" % path)
        self.newline = "\r\n" if crlf else "\n"
        self.trailing = text.endswith(self.newline)
        self.lines = []
        if text:
            if self.trailing:
                text = text[:-len(self.newline)]
            self.lines = text.split(self.newline)

    def render(self):
        text = self.newline.join(self.lines)
        return text + self.newline if self.lines and self.trailing else text

    def write(self, text):
        data = text.encode("utf-8")
        with open(self.path, "wb") as handle:
            for i in range(0, len(data), CHUNK_SIZE):
                handle.write(data[i:i + CHUNK_SIZE])
            handle.flush()

    def rows(self, ids, first, last):
        last = min(last, len(self.lines))
        return ["%s|%s" % (ids[i - 1], self.lines[i - 1]) for i in range(max(first, 1), last + 1)]


class LineIds:
    ID_LENGTH, ID_ALPHABET = 4, string.digits + string.ascii_lowercase
    SID = re.compile("[0-9a-z]{%d}" % ID_LENGTH)

    def __init__(self):
        self.ids = []
        self.hashes = []
        self.noise = int.from_bytes(os.urandom(8), "big")
        self.issued = 0

    def sync(self, lines):
        hashes = [hash(line) for line in lines]
        if hashes != self.hashes:
            self.ids = self._carry(lines, hashes)
            self.hashes = hashes

    def splice(self, lo, hi, lines):
        taken = set(self.ids)
        self.ids[lo:hi] = [self._fresh(line, taken) for line in lines]
        self.hashes[lo:hi] = [hash(line) for line in lines]

    def span(self, id_text, path):
        parts = id_text.split("..")
        if len(parts) > 2:
            raise Exception("invalid range '%s'" % id_text.strip())
        lo, hi = self.locate(parts[0], path), self.locate(parts[-1], path)
        if lo > hi:
            raise Exception("invalid range '%s': %s is below %s"
                            % (id_text.strip(), parts[0].strip().lower(), parts[-1].strip().lower()))
        return lo, hi

    def locate(self, text, path):
        anchor = text.strip().lower()
        if anchor == "start":
            return 0
        if anchor == "end":
            return len(self.ids) - 1
        if anchor in self.ids:
            return self.ids.index(anchor)
        if not self.SID.fullmatch(anchor):
            raise Exception("invalid anchor '%s'; copy an id from read output (for example '7qx2')" % text.strip())
        raise Exception("line id '%s' is not in %s: its line was changed or deleted, or the id comes from another file. "
                        "Read %s with ids=true for current ids." % (anchor, path, path))

    def _fresh(self, text, taken):
        base = len(self.ID_ALPHABET)
        while True:
            self.issued = self.issued + 1
            value = hash((self.noise, self.issued, text)) & 0xFFFFFFFFFFFF
            chars = []
            for _ in range(self.ID_LENGTH):
                value, digit = divmod(value, base)
                chars.append(self.ID_ALPHABET[digit])
            sid = "".join(chars)
            if sid not in taken:
                taken.add(sid)
                return sid

    def _carry(self, lines, hashes):
        old_ids = self.ids
        old = self.hashes
        limit = min(len(old), len(hashes))
        head = 0
        while head < limit and old[head] == hashes[head]:
            head = head + 1
        tail = 0
        while tail < limit - head and old[len(old) - 1 - tail] == hashes[len(hashes) - 1 - tail]:
            tail = tail + 1
        ids = old_ids[:head] + [None] * (len(hashes) - head - tail) + old_ids[len(old) - tail:]
        middle_old = old[head:len(old) - tail]
        middle_new = hashes[head:len(hashes) - tail]
        same = []
        if len(middle_old) == len(middle_new):
            same = [k for k in range(len(middle_new)) if middle_old[k] == middle_new[k]]
        if middle_new and len(same) * 2 >= len(middle_new):
            for k in same:
                ids[head + k] = old_ids[head + k]
        elif middle_old and middle_new:
            matcher = difflib.SequenceMatcher(a=middle_old, b=middle_new, autojunk=False)
            for i, j, n in matcher.get_matching_blocks():
                ids[head + j:head + j + n] = old_ids[head + i:head + i + n]
        taken = set(old_ids)
        for k in range(len(ids)):
            if ids[k] is None:
                ids[k] = self._fresh(lines[k], taken)
        return ids


class Files:
    MAX_READ_CHARS, MAX_DIFF_CHARS, TRACKED_LINES, ID_FORMAT = 24_000, 12_000, 200_000, "'<id>|<content>'"
    READ_CLIPPED = "\n... (truncated: the file is too large to show at once; page through the rest with read offset/limit)"
    DIFF_CLIPPED = "\n... (diff truncated)"

    def __init__(self, shell):
        self.shell = shell
        self.tracked = {}

    def read(self, args):
        path = self._path(args, "file_path", "file")
        offset, limit, show_ids = args.positive_int("offset"), args.positive_int("limit"), args.flag("ids")
        file = TextFile(path)
        total = len(file.lines)
        if 0 < total <= offset:
            raise Exception("offset %s is past the end of the file (%s lines)." % (offset, total))
        parts = []
        if show_ids:
            if path not in self.tracked:
                parts.append("%s (ids: each line is %s):" % (path, self.ID_FORMAT))
            ids = self._track(path, file.lines)
        if total == 0:
            return "\n".join(parts + ["(the file is empty: 0 lines)"])
        end = min(offset + limit, total) if limit else total
        if offset > 0 or end < total:
            parts.append("Showing lines %s-%s of %s total lines." % (offset + 1, end, total))
        body = "\n".join(file.rows(ids.ids, offset + 1, end) if show_ids else file.lines[offset:end])
        parts.append(terminal.clip(body, self.MAX_READ_CHARS, self.READ_CLIPPED))
        return "\n".join(parts)

    def edit(self, action, args):
        path = self._path(args, "file", "file_path")
        file = TextFile(path)
        if path not in self.tracked and file.lines:
            raise Exception("%s denied: no line ids are held for %s. Read it with ids=true, then retry "
                            "with an id copied from that output." % (action, path))
        before = file.render()
        ids = self._track(path, file.lines)
        if file.lines:
            lo, hi, new, success = self._plan(action, args, ids, path)
        elif action == "delete":
            raise Exception("%s is empty; there is nothing to delete" % path)
        else:
            lo, hi, new, success = 0, 0, self._new_lines(args), "Added the file's initial content."
            file.trailing = True
        file.lines[lo:hi] = new
        after = file.render()
        if after == before:
            return ("%s\nNo changes were made: the content was byte-identical to the targeted lines, so %s is unchanged. "
                    "Send different content if you meant to change it." % (success, path))
        file.write(after)
        ids.splice(lo, hi, new)
        self._keep(path, ids)
        parts = [success, "Diff:", self._unified_diff(path, before, after)]
        if file.lines:
            first = max(lo - 2, 0) + 1
            last = min(len(file.lines), max(lo + len(new) + 2, first))
            parts.append("Current %s (ids, lines %s-%s: each line is %s):" % (path, first, last, self.ID_FORMAT))
            parts.extend(file.rows(ids.ids, first, last))
        return "\n".join(parts)

    def _plan(self, action, args, ids, path):
        id_text = args.required("id", "; file mutations cannot create files or target empty files. "
                                "Use %s to create the file, then read with ids=true before editing." % SHELL)
        new = [] if action == "delete" else self._new_lines(args)
        if action != "insert":
            lo, hi = ids.span(id_text, path)
            return lo, hi + 1, new, self._lines_message("Deleted" if action == "delete" else "Edited", lo + 1, hi + 1)
        if ".." in id_text:
            left, right = id_text.strip().split("..", 1)
            raise Exception("insert requires one line id, not a range. "
                            "Use id '%s' with position='after', or id '%s' with position='before'." % (left, right))
        position = str(args.get("position") or "after").lower()
        if position not in ("before", "after"):
            raise Exception("'position' must be 'before' or 'after'")
        at = ids.locate(id_text, path)
        if position == "after":
            at = at + 1
        return at, at, new, self._lines_message("Inserted", at + 1, at + len(new))

    def _lines_message(self, verb, first, last):
        if first == last:
            return "%s line %s." % (verb, first)
        return "%s lines %s-%s." % (verb, first, last)

    def _new_lines(self, args):
        content = args.get("content")
        if not isinstance(content, str):
            raise Exception("'content' is required")
        return content.splitlines() or [""]

    def _path(self, args, key, alias):
        value = args.text(key) or args.text(alias) or args.required(key)
        return os.path.normpath(os.path.join(self.shell.cwd, os.path.expanduser(value)))

    def _track(self, path, lines):
        if len(lines) > self.TRACKED_LINES:
            raise Exception("the file has %s lines; line ids are kept for files of up to %s lines. "
                            "Change it with %s instead." % (len(lines), self.TRACKED_LINES, SHELL))
        ids = self.tracked.get(path) or LineIds()
        ids.sync(lines)
        self._keep(path, ids)
        return ids

    def _keep(self, path, ids):
        self.tracked.pop(path, None)
        self.tracked[path] = ids
        while len(self.tracked) > 1 and (len(self.tracked) > TRACKED_FILES
                                         or sum([len(held.ids) for held in self.tracked.values()]) > self.TRACKED_LINES):
            del self.tracked[next(iter(self.tracked))]

    def _unified_diff(self, path, before, after):
        diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                              fromfile="a/%s" % path, tofile="b/%s" % path, lineterm=""))
        return terminal.clip(diff, self.MAX_DIFF_CHARS, self.DIFF_CLIPPED)


class Client:
    ANTHROPIC_VERSION, MAX_TOKENS, CONNECT_TIMEOUT, WRITE_BUF_LIMIT = "2023-06-01", 64000, 20.0, 1048576
    BACKOFF, RETRY_BUDGET = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0), 600

    def __init__(self, url, auth=None):
        self.url, self.wire = url, url.rsplit("/", 1)[-1]
        scheme, _, rest = url.partition("://")
        self.host, _, self.path = rest.partition("/")
        self.connection = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        self.headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": "ucoder"}
        self.headers.update(auth or {})
        if self.wire == "messages":
            self.headers["anthropic-version"] = self.ANTHROPIC_VERSION

    def respond(self, body):
        attempt = 0
        start = time.monotonic()
        while True:
            attempt = attempt + 1
            try:
                return self._attempt(body)
            except Exception as err:
                if not getattr(err, "retry", False):
                    raise
                failing = time.monotonic() - start
                delay = self.BACKOFF[min(attempt, len(self.BACKOFF)) - 1]
                if failing + delay > self.RETRY_BUDGET:
                    raise self._api_error("gave up after %.0fs of failed attempts: %s" % (failing, err))
                terminal.warn("%s; retrying in %.0fs (attempt %s)" % (err, delay, attempt + 1))
                time.sleep(delay)

    def _attempt(self, body):
        if self.wire == "completions":
            body = self._chat_request(body)
            texts, slots, order = [], {}, []
        elif self.wire == "messages":
            body, blocks = self._messages_request(body), []
        for event in self._events(body):
            kind = event.get("type", "")
            if self.wire == "messages":
                if kind == "content_block_start":
                    blocks.append(dict(event["content_block"]))
                    if blocks[-1]["type"] == "thinking":
                        terminal.stream({"type": "response.output_item.added", "item": {"type": "reasoning"}})
                elif kind == "content_block_delta":
                    block = blocks[event["index"]]
                    for key, value in event["delta"].items():
                        if key != "type":
                            block[key] = block.get(key, "") + value
                    if "text" in event["delta"]:
                        terminal.stream({"type": "response.output_text.delta", "delta": event["delta"]["text"]})
                elif kind == "message_delta" and event["delta"].get("stop_reason") == "refusal":
                    raise self._api_error("the model declined the request (stop_reason: refusal)")
                elif kind == "message_stop":
                    return self._messages_output(blocks)
                elif kind == "error":
                    raise self._api_error(self._describe(event), retry=True)
                continue
            if self.wire == "completions":
                if kind == "chat.tool":
                    slots, order = self._slot(slots, order, event)
                    continue
                if kind == "chat.done":
                    output = []
                    if "".join(texts):
                        output.append({"type": "message", "content": [{"type": "output_text", "text": "".join(texts)}]})
                    for index, slot in enumerate(order):
                        if slot["name"] or slot["arguments"]:
                            output.append({"type": "function_call", "call_id": slot["call_id"] or "call_%s" % index,
                                           "name": slot["name"], "arguments": slot["arguments"] or "{}"})
                    return {"output": output, "status": "completed"}
                texts.append(event.get("delta", ""))
                terminal.stream(event)
                continue
            if kind in ("response.completed", "response.incomplete"):
                return event["response"]
            if kind == "response.failed":
                raise self._api_error(self._describe(event["response"].get("error")))
            if kind == "error":
                raise self._api_error(self._describe(event))
            terminal.stream(event)
        raise self._api_error("the stream ended before the response completed", retry=True)

    def _events(self, body):
        connection = self.connection(self.host, timeout=self.CONNECT_TIMEOUT)
        try:
            connection.request("POST", "/" + self.path, json.dumps(body).encode(), self.headers)
            sock = connection.sock
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as err:
            connection.close()
            refused = type(err).__name__ == "SSLCertVerificationError"
            raise self._api_error("cannot reach %s: %s" % (self.url, err), retry=not refused)
        sock.settimeout(TIMEOUT)
        data, plain, size, seen = [], [], 0, False
        try:
            with response:
                if response.status >= 300:
                    retry = response.status == 429 or response.status >= 500
                    raise self._api_error("HTTP %s: %s" % (response.status, self._describe(response.read())), retry=retry)
                for line in self._body_lines(response):
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif line:
                        if not seen and size < self.WRITE_BUF_LIMIT:
                            plain.append(line)
                            size = size + len(line)
                    elif data:
                        payload = "\n".join(data)
                        data = []
                        if payload == "[DONE]":
                            return
                        event = JsonObject.parse(payload)
                        if event is None:
                            continue
                        seen = True
                        if self.wire == "completions":
                            for event in self._chat(event) or []:
                                yield event
                            continue
                        yield event
        except (OSError, http.client.HTTPException) as err:
            raise self._api_error("connection lost mid-stream: %s" % err, retry=True)
        finally:
            connection.close()
        reply = None if seen else JsonObject.parse("\n".join(plain))
        if reply is None:
            return
        if isinstance(reply.get("error"), dict):
            raise self._api_error(self._describe(reply))
        if isinstance(reply.get("output"), list):
            for item in reply["output"]:
                if isinstance(item, dict) and item.get("type") == "message":
                    yield {"type": "response.output_item.added", "item": {"type": "message"}}
                    for part in item.get("content") or []:
                        if isinstance(part, dict) and part.get("text"):
                            yield {"type": "response.output_text.delta", "delta": part["text"]}
            yield {"type": "response.completed", "response": dict(reply)}

    def _chat(self, chunk):
        if isinstance(chunk.get("error"), dict):
            raise self._api_error(self._describe(chunk))
        choices = chunk.get("choices") or []
        if not choices:
            return []
        choice, delta = choices[0], (choices[0].get("delta") or {})
        if choice.get("finish_reason"):
            return [{"type": "chat.done", "response": dict(chunk)}]
        events = []
        for index, call in enumerate(delta.get("tool_calls") or []):
            function = call.get("function") or {}
            if function.get("name") or function.get("arguments"):
                events.append({"type": "chat.tool", "index": index, "call_id": call.get("id") or "",
                               "name": function.get("name") or "", "arguments": function.get("arguments") or ""})
        if events:
            return events
        if delta.get("content"):
            return [{"type": "response.output_text.delta", "delta": delta["content"]}]
        if delta.get("reasoning") or delta.get("reasoning_content"):
            return [{"type": "response.output_item.added", "item": {"type": "reasoning"}}]
        return []

    def _slot(self, slots, order, event):
        slot = slots.get(event["index"])
        if slot is None or (event["call_id"] and event["call_id"] != slot["call_id"]):
            slot = {"index": event["index"], "call_id": event["call_id"], "name": "", "arguments": ""}
            slots[event["index"]] = slot
            order.append(slot)
        slot["call_id"] = slot["call_id"] or event["call_id"]
        slot["name"] = slot["name"] or event["name"]
        slot["arguments"] = slot["arguments"] + (event["arguments"] or "")
        return slots, order

    def _chat_request(self, body):
        messages = [{"role": "system", "content": body["instructions"]}] + self._chat_messages(body["input"])
        tools = [{"type": "function", "function": {key: tool[key] for key in ("name", "description", "parameters")}}
                 for tool in body["tools"]]
        return {"model": body["model"], "messages": messages, "tools": tools, "stream": True,
                "stream_options": {"include_usage": True}}

    def _chat_messages(self, items):
        messages = []
        for item in items:
            role = item.get("role")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": "".join([part.get("text", "") for part in item.get("content") or []])})
                continue
            if item.get("type") == "function_call":
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": [{"id": item["call_id"], "type": "function",
                                                 "function": {"name": item["name"], "arguments": item["arguments"]}}]})
                continue
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        return messages

    def _messages_request(self, body):
        messages = []
        for item in body["input"]:
            if item.get("type") == "blocks":
                messages.append({"role": "assistant", "content": item["content"]})
            elif item.get("type") == "function_call_output":
                result = {"type": "tool_result", "tool_use_id": item["call_id"], "content": item["output"]}
                if messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                    messages[-1]["content"].append(result)
                else:
                    messages.append({"role": "user", "content": [result]})
            elif item.get("role") == "user":
                messages.append({"role": "user", "content": item["content"][0]["text"]})
        tools = [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
                 for tool in body["tools"]]
        return {"model": body["model"], "max_tokens": self.MAX_TOKENS, "system": body["instructions"], "tools": tools,
                "messages": messages, "stream": True}

    def _messages_output(self, blocks):
        output = [{"type": "blocks", "content": blocks}]
        for block in blocks:
            if block["type"] == "text":
                output.append({"type": "message", "content": [{"type": "output_text", "text": block["text"]}]})
            elif block["type"] == "tool_use":
                arguments = block.pop("partial_json", "") or json.dumps(block.get("input") or {})
                block["input"] = dict(JsonObject.parse(arguments) or {})
                output.append({"type": "function_call", "call_id": block["id"], "name": block["name"], "arguments": arguments})
        return {"output": output, "status": "completed"}

    def _body_lines(self, response):
        for raw in iter(response.readline, b""):
            yield raw.decode("utf-8", "replace").rstrip("\r\n")
        yield ""

    def _api_error(self, message, retry=False):
        err = Exception(message)
        err.retry = retry
        return err

    def _describe(self, error):
        if isinstance(error, bytes):
            try:
                error = json.loads(error)
            except ValueError:
                return error.decode("utf-8", "replace").strip() or "no detail"
        if isinstance(error, dict):
            error = error.get("error", error)
            if isinstance(error, dict):
                return "%s: %s" % (error.get("code") or error.get("type") or "error", error.get("message", ""))
        return str(error or "no detail")


class Agent:
    CONTINUATIONS = 5
    SYSTEM_PROMPT = """\
You are a concise coding agent working directly on the user's machine.
Environment: {system}. Working directory: {cwd}.

Method: look before changing (read, or {tool} for listings and searches). For existing files, \
read with ids=true and edit with replace, insert or delete rather than {tool} rewrites. Run or \
test what you changed. Be brief: short answers, no restating tool output, no plans longer than \
the work. When the task is done and verified, reply without a tool call with a short summary.

Memories:
{memories}"""

    def __init__(self, client, shell, model):
        self.client = client
        self.shell = shell
        self.files = Files(shell)
        self.store = Store(shell.cwd)
        self.model = model
        self.system = self._system_prompt(shell.cwd)
        self.history = []

    @interruptible
    def run_turn(self, text):
        self._answer_open_calls("error: interrupted by user")
        self._say("user", text)
        continuations = 0
        while True:
            response = self._complete()
            calls = self._record(response.get("output") or [])
            if calls:
                continuations = 0
                for call in calls:
                    self._run_call(call)
                continue
            if response.get("status") != "incomplete":
                return
            reason = (response.get("incomplete_details") or {}).get("reason", "no reason given")
            continuations = continuations + 1
            if continuations > self.CONTINUATIONS:
                terminal.warn("stopped early: %s (already continued %s times)" % (reason, self.CONTINUATIONS))
                return
            terminal.warn("response cut off (%s); continuing (%s/%s) …" % (reason, continuations, self.CONTINUATIONS))
            self._say("user", "Continue. Pick up exactly where you stopped.")

    def _say(self, role, text):
        kind = "input_text" if role == "user" else "output_text"
        self.history.append({"role": role, "content": [{"type": kind, "text": text}]})

    def _answer(self, call_id, output):
        self.history.append({"type": "function_call_output", "call_id": call_id, "output": output})

    def _answer_open_calls(self, result):
        answered = set([item.get("call_id") for item in self.history if item.get("type") == "function_call_output"])
        for item in list(self.history):
            if item.get("type") == "function_call" and item["call_id"] not in answered:
                self._answer(item["call_id"], result)

    def _complete(self):
        body = {"model": self.model, "instructions": self.system, "input": self.history, "tools": TOOLS, "stream": True}
        try:
            return self.client.respond(body)
        finally:
            terminal.stream_to(terminal.STREAM_IDLE, "\n")

    def _record(self, output):
        calls = []
        for item in output:
            kind = item.get("type")
            if kind == "message":
                text = "".join([part.get("text") or part.get("refusal") or "" for part in item.get("content") or []])
                if text:
                    self._say("assistant", text)
            elif kind == "blocks":
                self.history.append(item)
            elif kind == "function_call":
                call_id = item.get("call_id") or item.get("id") or "call_%s" % len(self.history)
                call = {"type": "function_call", "call_id": call_id, "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}"}
                self.history.append(call)
                calls.append(call)
        return calls

    def _run_call(self, call):
        args = JsonObject.parse(call["arguments"])
        terminal.tool_call(call["name"], args or JsonObject())
        self.shell.status = None
        if args is None:
            result = "error: the arguments are not a JSON object: %r. Call the tool again with valid JSON." % (
                call["arguments"][:200])
        else:
            result = self._run_tool(call["name"], args)
        shown = [self.shell.status or result]
        if call["name"] == "memory" and not result.startswith("error:"):
            lines = result.split("\n")
            shown = (args.text("summary") or (lines[1] if len(lines) > 1 else result)).split("\n")
        for line in shown[:-1]:
            terminal.tool_output(line)
        terminal.tool_result(shown[-1])
        self._answer(call["call_id"], result)

    def _run_tool(self, name, args):
        try:
            if name == SHELL:
                return self.shell.run(args)
            if name == "read":
                return self.files.read(args)
            if name in ("replace", "insert", "delete"):
                return self.files.edit(name, args)
            if name == "memory":
                return self._run_memory(args)
        except Exception as err:
            return "error: %s" % terminal.explain(err)
        return "error: unknown tool %r. Available: %s" % (name, ", ".join([tool["name"] for tool in TOOLS]))

    def _run_memory(self, args):
        read, write = args.text("read"), args.text("write")
        if read and write:
            raise Exception("pass either 'read' or 'write', not both")
        if read:
            return self.store.recall(read)
        if not write:
            raise Exception("pass {\"read\": \"<name>\"} to read or {\"write\": \"<name>\", \"summary\": ..., \"content\": ...} to write")
        summary = args.required("summary", " when writing a memory").strip()
        content = args.required("content", " when writing a memory")
        key = self.store.memorize(write, args.text("title") or write, summary, content)
        return "Wrote memory %s." % key

    def _system_prompt(self, cwd):
        listing = "\n".join(["- %s: %s" % entry for entry in self.store.listing()])
        return self.SYSTEM_PROMPT.format(system=OS_DESCRIPTION, tool=SHELL, cwd=cwd, memories=listing or "(no memories stored)")


@interruptible
def main(argv=None):
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    parser = argparse.ArgumentParser(
        prog="ucoder", description="ucoder - AI coding assistant with a command tool and file editing by line id.")
    parser.add_argument("prompt", nargs="?", help="run this one turn and exit (default: an interactive session)")
    backend = parser.add_mutually_exclusive_group()
    for name, (label, url, model) in BACKENDS.items():
        backend.add_argument("--" + name, nargs="?", const=(name, model), type=lambda value, name=name: (name, value),
                             dest="backend", metavar="MODEL", help="use %s (default model %s)" % (label, model))
    parser.add_argument("--host", default=OLLAMA_HOST, help="Ollama host or URL (default %s)" % OLLAMA_HOST)
    parser.add_argument("--cwd", default=os.getcwd(), metavar="DIR", help="initial working directory (default: here)")
    args = parser.parse_args(argv)
    name, model = args.backend or ("meta", BACKENDS["meta"][2])
    url = BACKENDS[name][1]
    api_key = os.environ.get("MODEL_API_KEY")
    if name == "ollama":
        host = args.host.strip().rstrip("/")
        client = Client((host if "://" in host else "http://" + host) + url)
    elif not api_key:
        terminal.error("set MODEL_API_KEY to your API key for --%s" % name)
        return EX_CONFIG
    else:
        client = Client(url, {"x-api-key": api_key} if name == "anthropic" else {"Authorization": "Bearer " + api_key})
    if not os.path.isdir(args.cwd):
        terminal.error("not a directory: %s" % args.cwd)
        return EX_USAGE
    shell = Shell(args.cwd)
    try:
        agent = Agent(client, shell, model)
        terminal.banner(model, shell.cwd)
        if args.prompt:
            return agent.run_turn(args.prompt) or EX_OK
        terminal.write("\n")
        while (line := agent.store.ask()) != INTERRUPTED:
            if not line:
                continue
            try:
                if agent.run_turn(line):
                    terminal.warn("interrupted")
            except Exception as err:
                terminal.error(err)
            terminal.write("\n")
        return EX_OK
    except Exception as err:
        terminal.error(err)
        return EX_SOFTWARE
    finally:
        shell.close()


if __name__ == "__main__":
    raise SystemExit(main())
