#!/usr/bin/env python3
"""
Script Runner - A Python GUI tool for running scripts with tabbed interface
Each tab can run a separate script and display real-time terminal output.

Features:
- Multiple tabs for running different scripts
- Real-time output with ANSI color support
- Environment variables per script
- Virtual environment activation
- Workspace save/load with auto-save on exit
- Output search (Ctrl+F) and export
- Modern UI with ttkbootstrap
- NEW: High Performance Batch-Logging
- NEW: Automated Keyword Highlighting (INFO, ERROR, etc.)
"""

import os
import re
import sys
import json
import queue
import shlex
import subprocess
import threading
import time
import psutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Callable, Tuple


# Try to use ttkbootstrap for modern UI, fall back to ttk if not available
try:
    import ttkbootstrap as ttk

    HAS_BOOTSTRAP = True
except ImportError:
    from tkinter import ttk

    HAS_BOOTSTRAP = False

import tkinter as tk
from tkinter import filedialog

# =============================================================================
# CONFIGURATION
# =============================================================================

MAX_OUTPUT_LINES = 10000
TARGET_OUTPUT_LINES = 9000
TRIM_CHECK_INTERVAL_LINES = 250
SEARCH_HIGHLIGHT_COLOR = "#ffd700"
SEARCH_DEBOUNCE_MS = 200
SEARCH_MAX_MATCHES = 1000
UI_UPDATE_INTERVAL_MS = 50
PRODUCER_BATCH_MAX_LINES = 100
PRODUCER_BATCH_MAX_CHARS = 8192

LOG_PATTERN = re.compile(
    r"(?P<error>ERROR|CRITICAL|FAIL|EXCEPTION|Traceback|INTERNAL SERVER ERROR)|"
    r"(?P<warning>WARNING|WARN|DEPRECATION)|"
    r"(?P<info>INFO|DEBUG|at_api|Query|GET|POST|PUT|DELETE)|"
    r"(?P<success>SUCCESS|OK|Succeeded|Finished|Connected)",
    re.IGNORECASE,
)

# =============================================================================
# PYTHON INTERPRETER DETECTION
# =============================================================================

_detected_python = None


def get_default_python() -> str:
    """Detect the default Python interpreter for this system."""
    global _detected_python

    if _detected_python:
        return _detected_python

    if sys.platform == "win32":
        candidates = ["python", "python3"]
    else:
        candidates = ["python3", "python"]

    for py in candidates:
        try:
            result = subprocess.run(
                [py, "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                _detected_python = py
                return py
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

    _detected_python = "python" if sys.platform == "win32" else "python3"
    return _detected_python


# =============================================================================
# ANSI COLOR HANDLING
# =============================================================================


class ANSIColorConverter:
    """Converts ANSI color codes to tkinter tags for colored output."""

    COLORS = {
        "30": "#2e3436",
        "31": "#ef4444",
        "32": "#22c55e",
        "33": "#eab308",
        "34": "#3b82f6",
        "35": "#a855f7",
        "36": "#06b6d4",
        "37": "#d1d5db",
        "90": "#6b7280",
        "91": "#f87171",
        "92": "#4ade80",
        "93": "#facc15",
        "94": "#60a5fa",
        "95": "#c084fc",
        "96": "#22d3ee",
        "97": "#f9fafb",
    }

    BACKGROUND_COLORS = {
        "40": "#1f2937",
        "41": "#7f1d1d",
        "42": "#14532d",
        "43": "#713f12",
        "44": "#1e3a8a",
        "45": "#581c87",
        "46": "#164e63",
        "47": "#374151",
    }

    ANSI_PATTERN = re.compile(r"\x1b\[([0-9;]*)m")

    @classmethod
    def configure_tags(cls, text_widget: tk.Text, bg_color: str = "#1f2937"):
        """Configure text tags for all colors."""
        text_widget.tag_configure("default", foreground="#d1d5db", background=bg_color)

        for code, color in cls.COLORS.items():
            text_widget.tag_configure(
                f"fg_{code}", foreground=color, background=bg_color
            )

        for code, color in cls.BACKGROUND_COLORS.items():
            text_widget.tag_configure(
                f"bg_{code}", background=color, foreground="#d1d5db"
            )

        text_widget.tag_configure("bold", font=("Consolas", 10, "bold"))
        text_widget.tag_configure("italic", font=("Consolas", 10, "italic"))
        text_widget.tag_configure("underline", font=("Consolas", 10, "underline"))

        text_widget.tag_configure(
            "search_match", background=SEARCH_HIGHLIGHT_COLOR, foreground="black"
        )

        text_widget.tag_raise("sel")

    @classmethod
    def process_line(cls, text_widget: tk.Text, line: str):
        """Process a line with ANSI codes and insert with appropriate tags."""
        current_tag = "default"
        last_end = 0

        for match in cls.ANSI_PATTERN.finditer(line):
            text_before = line[last_end : match.start()]
            if text_before:
                text_widget.insert(tk.END, text_before, current_tag)

            codes = match.group(1).split(";")
            if codes == ["0"] or codes == [""]:
                current_tag = "default"
            else:
                for code in codes:
                    if code in cls.COLORS:
                        current_tag = f"fg_{code}"
                    elif code == "1":
                        current_tag = "bold"

            last_end = match.end()

        remaining = line[last_end:]
        if remaining:
            text_widget.insert(tk.END, remaining, current_tag)


# =============================================================================
# CUSTOM DIALOG
# =============================================================================


def show_custom_message(parent, title: str, message: str, msg_type: str = "info"):
    """Show a custom message dialog with a copy button."""
    type_config = {
        "error": {"icon": "✗", "color": "#ef4444", "bg": "#7f1d1d"},
        "warning": {"icon": "⚠", "color": "#eab308", "bg": "#713f12"},
        "info": {"icon": "ℹ", "color": "#3b82f6", "bg": "#1e3a8a"},
    }
    config = type_config.get(msg_type, type_config["info"])

    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.geometry("500x220")
    dialog.transient(parent)
    dialog.grab_set()

    bg_color = "#1f2937"
    dialog.configure(background=bg_color)

    main_frame = tk.Frame(dialog, background=bg_color)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

    title_frame = tk.Frame(main_frame, background=bg_color)
    title_frame.pack(fill=tk.X, pady=(0, 10))

    icon_label = tk.Label(
        title_frame,
        text=config["icon"],
        foreground=config["color"],
        background=bg_color,
        font=("Arial", 18, "bold"),
    )
    icon_label.pack(side=tk.LEFT, padx=(0, 10))

    title_label = tk.Label(
        title_frame,
        text=title,
        foreground=config["color"],
        background=bg_color,
        font=("Arial", 14, "bold"),
    )
    title_label.pack(side=tk.LEFT)

    msg_container = tk.Frame(main_frame, background=config["color"])
    msg_container.pack(fill=tk.BOTH, expand=True, pady=(0, 15))

    msg_text = tk.Text(
        msg_container,
        height=5,
        wrap=tk.WORD,
        background="#374151",
        foreground="#f9fafb",
        font=("Consolas", 10),
        padx=10,
        pady=10,
        borderwidth=0,
        highlightthickness=0,
    )
    msg_text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
    msg_text.insert("1.0", message)
    msg_text.configure(state=tk.DISABLED)

    btn_frame = tk.Frame(main_frame, background=bg_color)
    btn_frame.pack(fill=tk.X)

    def copy_message():
        parent.clipboard_clear()
        parent.clipboard_append(f"{title}\n\n{message}")
        copy_btn.configure(text="Copied!")
        dialog.after(1500, lambda: copy_btn.configure(text="Copy to Clipboard"))

    def close_dialog():
        dialog.grab_release()
        dialog.destroy()

    copy_btn = tk.Button(
        btn_frame,
        text="Copy to Clipboard",
        command=copy_message,
        background="#374151",
        foreground="#f9fafb",
        activebackground="#4b5563",
        activeforeground="white",
        font=("Arial", 10),
        padx=15,
        pady=5,
        relief=tk.FLAT,
    )
    copy_btn.pack(side=tk.LEFT, padx=(0, 10))

    close_btn = tk.Button(
        btn_frame,
        text="Close",
        command=close_dialog,
        background="#3b82f6",
        foreground="white",
        activebackground="#2563eb",
        activeforeground="white",
        font=("Arial", 10),
        padx=20,
        pady=5,
        relief=tk.FLAT,
    )
    close_btn.pack(side=tk.LEFT)

    dialog.update_idletasks()
    x = parent.winfo_x() + (parent.winfo_width() - dialog.winfo_width()) // 2
    y = parent.winfo_y() + (parent.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{x}+{y}")

    dialog.focus_set()
    dialog.bind("<Escape>", lambda e: close_dialog())
    dialog.bind("<Return>", lambda e: close_dialog())

    parent.wait_window(dialog)


# =============================================================================
# SEARCH BAR WIDGET
# =============================================================================


class SearchBar(ttk.Frame):
    """A search bar widget for searching in text output."""

    def __init__(
        self,
        parent,
        text_widget: tk.Text,
        get_text_revision: Optional[Callable[[], int]] = None,
        **kwargs,
    ):
        super().__init__(parent, **kwargs)
        self.text_widget = text_widget
        self.get_text_revision = get_text_revision or (lambda: 0)
        self.search_matches: List[Tuple[str, str]] = []
        self.current_match_index = -1
        self.last_search_term = ""
        self.last_search_revision = -1
        self._search_after_id = None
        self._match_count_capped = False

        self._setup_ui()

    def _setup_ui(self):
        """Set up the search bar UI."""
        ttk.Label(self, text="Find:").pack(side=tk.LEFT, padx=(0, 5))

        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(self, textvariable=self.search_var, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 5))
        self.search_entry.bind("<Return>", self._search_next)
        self.search_entry.bind("<KeyRelease>", self._on_key_release)

        self.prev_btn = ttk.Button(self, text="<", width=3, command=self._search_prev)
        self.prev_btn.pack(side=tk.LEFT, padx=(0, 2))

        self.next_btn = ttk.Button(self, text=">", width=3, command=self._search_next)
        self.next_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.match_label = ttk.Label(self, text="No matches")
        self.match_label.pack(side=tk.LEFT, padx=(0, 10))

        self.close_btn = ttk.Button(self, text="X", width=3, command=self.hide)
        self.close_btn.pack(side=tk.LEFT)

        self.bind_all("<Escape>", lambda e: self.hide())

    def show(self):
        """Show the search bar and focus the entry."""
        self.pack(fill=tk.X, pady=(0, 5))
        self.search_entry.focus_set()
        self.search_entry.select_range(0, tk.END)
        self._schedule_search(immediate=True)

    def hide(self):
        """Hide the search bar and clear highlights."""
        self._cancel_pending_search()
        self.clear_highlights()
        self.pack_forget()

    def clear_highlights(self):
        """Remove all search highlights."""
        self.text_widget.tag_remove("search_match", "1.0", tk.END)

    def _cancel_pending_search(self):
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except tk.TclError:
                pass
            self._search_after_id = None

    def _schedule_search(self, immediate: bool = False):
        self._cancel_pending_search()
        delay = 0 if immediate else SEARCH_DEBOUNCE_MS
        self._search_after_id = self.after(delay, self._perform_search)

    def _reset_search_state(self):
        self.clear_highlights()
        self.search_matches = []
        self.current_match_index = -1
        self._match_count_capped = False

    def _on_key_release(self, event):
        """Handle key release for live search."""
        if event.keysym not in ["Return", "Escape", "Up", "Down", "Left", "Right"]:
            self._schedule_search()

    def _perform_search(self):
        """Perform the search and highlight matches."""
        self._search_after_id = None
        search_term = self.search_var.get().strip()
        current_revision = self.get_text_revision()

        if (
            search_term == self.last_search_term
            and current_revision == self.last_search_revision
        ):
            return

        self.last_search_term = search_term
        self.last_search_revision = current_revision
        self._reset_search_state()

        if not search_term:
            self.match_label.configure(text="No matches")
            return

        start_pos = "1.0"
        term_len = len(search_term)
        tag_add = self.text_widget.tag_add
        widget_search = self.text_widget.search

        while len(self.search_matches) < SEARCH_MAX_MATCHES:
            pos = widget_search(search_term, start_pos, tk.END, nocase=True, regexp=False)
            if not pos:
                break

            end_pos = f"{pos}+{term_len}c"
            tag_add("search_match", pos, end_pos)
            self.search_matches.append((pos, end_pos))
            start_pos = end_pos
        else:
            self._match_count_capped = True

        total = len(self.search_matches)
        if total > 0:
            self.current_match_index = 0
            self._go_to_match(0)
        else:
            self.match_label.configure(text="No matches")

    def _search_next(self, event=None):
        """Go to the next match."""
        if not self.search_matches:
            self._schedule_search(immediate=True)
            return

        self.current_match_index = (self.current_match_index + 1) % len(self.search_matches)
        self._go_to_match(self.current_match_index)

    def _search_prev(self, event=None):
        """Go to the previous match."""
        if not self.search_matches:
            self._schedule_search(immediate=True)
            return

        self.current_match_index = (self.current_match_index - 1) % len(self.search_matches)
        self._go_to_match(self.current_match_index)

    def _go_to_match(self, index: int):
        """Scroll to and highlight the match at the given index."""
        if 0 <= index < len(self.search_matches):
            pos, _ = self.search_matches[index]
            self.text_widget.see(pos)
            total = len(self.search_matches)
            suffix = "+" if self._match_count_capped else ""
            self.match_label.configure(text=f"{index + 1} of {total}{suffix}")


# =============================================================================
# PROCESS RUNNER
# =============================================================================


class ProcessRunner:
    """Kapselt die Prozess-Steuerung entkoppelt von der UI."""

    def __init__(
        self, cmd: List[str], cwd: Path, env: Dict[str, str], output_queue: queue.Queue
    ):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.output_queue = output_queue
        self.process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()

    def start(self):
        def run():
            try:
                self.process = subprocess.Popen(
                    self.cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    cwd=str(self.cwd),
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self.env,
                )

                stdout_thread = threading.Thread(
                    target=self._read_stream, args=(self.process.stdout,)
                )
                stderr_thread = threading.Thread(
                    target=self._read_stream, args=(self.process.stderr,)
                )
                stdout_thread.daemon = stderr_thread.daemon = True
                stdout_thread.start()
                stderr_thread.start()

                return_code = self.process.wait()
                stdout_thread.join(timeout=0.25)
                stderr_thread.join(timeout=0.25)
                self.output_queue.put(("complete", return_code))
            except Exception as e:
                self.output_queue.put(("error", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _read_stream(self, stream):
        if stream is None:
            return

        buffered_lines: List[str] = []
        buffered_chars = 0
        last_flush_time = time.monotonic()

        def flush_buffer():
            nonlocal buffered_chars, last_flush_time
            if buffered_lines:
                self.output_queue.put(("output_chunk", "".join(buffered_lines)))
                buffered_lines.clear()
                buffered_chars = 0
                last_flush_time = time.monotonic()

        try:
            for line in iter(stream.readline, ""):
                if self._stop_event.is_set():
                    break

                buffered_lines.append(line)
                buffered_chars += len(line)
                now = time.monotonic()

                if (
                    len(buffered_lines) >= PRODUCER_BATCH_MAX_LINES
                    or buffered_chars >= PRODUCER_BATCH_MAX_CHARS
                    or (buffered_chars <= 1024 and (now - last_flush_time) >= 0.05)
                    or len(buffered_lines) <= 3
                ):
                    flush_buffer()

            flush_buffer()
        except (OSError, ValueError):
            flush_buffer()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def send_input(self, text: str):
        """Sendet Text an stdin (für interaktive Prompts)."""
        if self.process and self.process.stdin:
            try:
                self.process.stdin.write(text + "\n")
                self.process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False
        return False

    def stop(self):
        """Beendet den gesamten Prozessbaum mittels psutil."""
        self._stop_event.set()
        if not self.process:
            return
        try:
            parent = psutil.Process(self.process.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


# =============================================================================
# SCRIPT TAB
# =============================================================================


class ScriptTab(ttk.Frame):
    """A single tab that can run a script and display output."""

    tab_counter = 0

    def __init__(self, parent, notebook, app, **kwargs):
        super().__init__(parent, **kwargs)
        self.notebook = notebook
        self.app = app
        self.process: Optional[subprocess.Popen] = None
        self.output_queue = queue.SimpleQueue()
        self.runner: Optional[ProcessRunner] = None
        self.is_running = False
        self.stop_output_thread = False
        self.advanced_expanded = False
        self.has_unsaved_changes = False
        self.output_line_count = 0
        self.lines_since_trim_check = 0
        self.output_revision = 0
        self._venv_cache: Dict[str, Optional[str]] = {}

        ScriptTab.tab_counter += 1
        self.tab_name = f"Script {ScriptTab.tab_counter}"

        self._setup_ui()
        self._start_throttled_handler()

    def _setup_ui(self):
        """Set up the tab's user interface."""
        main_container = ttk.Frame(self)
        main_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        control_frame = ttk.Frame(main_container)
        control_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(control_frame, text="Script:").pack(side=tk.LEFT, padx=(0, 5))

        self.script_path_var = tk.StringVar()
        self.script_entry = ttk.Entry(control_frame, textvariable=self.script_path_var)
        self.script_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        ttk.Button(control_frame, text="Browse", command=self._browse_script).pack(
            side=tk.LEFT, padx=(0, 10)
        )
        self.run_btn = ttk.Button(
            control_frame, text="[>] Run", command=self._run_script, width=8
        )
        self.run_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = ttk.Button(
            control_frame,
            text="[ ] Stop",
            command=self._stop_script,
            width=8,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 5))

        ttk.Button(
            control_frame, text="[X] Clear", command=self._clear_output, width=8
        ).pack(side=tk.LEFT)

        args_frame = ttk.Frame(main_container)
        args_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(args_frame, text="Arguments:").pack(side=tk.LEFT, padx=(0, 5))

        self.args_var = tk.StringVar()
        self.args_entry = ttk.Entry(args_frame, textvariable=self.args_var)
        self.args_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        self.control_frame2 = ttk.Frame(main_container)
        self.control_frame2.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(self.control_frame2, text="Interpreter:").pack(
            side=tk.LEFT, padx=(0, 5)
        )

        self.interpreter_var = tk.StringVar(value=get_default_python())
        interpreter_combo = ttk.Combobox(
            self.control_frame2,
            textvariable=self.interpreter_var,
            width=12,
            values=["python", "python3", "bash", "sh", "node", "ruby", "perl"],
        )
        interpreter_combo.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(self.control_frame2, text="Working Dir:").pack(
            side=tk.LEFT, padx=(0, 5)
        )

        self.working_dir_var = tk.StringVar()
        ttk.Entry(self.control_frame2, textvariable=self.working_dir_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5)
        )

        ttk.Button(
            self.control_frame2,
            text="Browse",
            command=self._browse_working_dir,
            width=8,
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.advanced_btn = ttk.Button(
            self.control_frame2,
            text="[>] Advanced",
            command=self._toggle_advanced,
            width=12,
        )
        self.advanced_btn.pack(side=tk.LEFT)

        self.advanced_frame = ttk.Frame(main_container)

        venv_frame = ttk.Frame(self.advanced_frame)
        venv_frame.pack(fill=tk.X, pady=(5, 5))

        ttk.Label(venv_frame, text="Virtual Env:").pack(side=tk.LEFT, padx=(0, 5))

        self.venv_path_var = tk.StringVar()
        ttk.Entry(venv_frame, textvariable=self.venv_path_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5)
        )

        ttk.Button(venv_frame, text="Browse", command=self._browse_venv, width=8).pack(
            side=tk.LEFT, padx=(0, 10)
        )

        self.auto_venv_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            venv_frame, text="Auto-detect", variable=self.auto_venv_var
        ).pack(side=tk.LEFT, padx=(0, 15))

        self.run_as_module_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            venv_frame, text="Run as module (-m)", variable=self.run_as_module_var
        ).pack(side=tk.LEFT)

        env_label_frame = ttk.Frame(self.advanced_frame)
        env_label_frame.pack(fill=tk.X, pady=(10, 2))

        ttk.Label(
            env_label_frame, text="Environment Variables (KEY=VALUE, one per line):"
        ).pack(side=tk.LEFT)
        ttk.Button(
            env_label_frame, text="Load .env", command=self._load_env_file, width=10
        ).pack(side=tk.RIGHT)

        env_text_frame = ttk.Frame(self.advanced_frame)
        env_text_frame.pack(fill=tk.X, pady=(0, 5))

        self.env_text = tk.Text(
            env_text_frame,
            height=5,
            wrap=tk.WORD,
            background="#374151",
            foreground="#d1d5db",
            insertbackground="white",
            font=("Consolas", 9),
        )

        env_scroll = ttk.Scrollbar(
            env_text_frame, orient=tk.VERTICAL, command=self.env_text.yview
        )
        self.env_text.configure(yscrollcommand=env_scroll.set)

        self.env_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        env_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        status_frame = ttk.Frame(main_container)
        status_frame.pack(fill=tk.X, pady=(5, 5))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=tk.LEFT)

        self.runtime_var = tk.StringVar(value="")
        ttk.Label(status_frame, textvariable=self.runtime_var).pack(side=tk.RIGHT)

        self.search_bar = SearchBar(main_container, None, self._get_output_revision)  # Will set text widget later

        output_frame = ttk.Frame(main_container)
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.output_text = tk.Text(
            output_frame,
            wrap=tk.WORD,
            background="#1f2937",
            foreground="#d1d5db",
            insertbackground="white",
            selectbackground="#3b82f6",
            selectforeground="white",
            inactiveselectbackground="#3b82f6",
            font=("Consolas", 10),
            undo=False,
            autoseparators=False,
            state=tk.NORMAL,
        )

        ANSIColorConverter.configure_tags(self.output_text)

        self.search_bar.text_widget = self.output_text

        def block_edit(event):
            if event.state & 0x4:
                if event.keysym.lower() in ["c", "a"]:
                    return None
            if event.keysym in ["BackSpace", "Delete", "Return", "Tab"]:
                return "break"
            if len(event.char) > 0 and event.char.isprintable():
                return "break"
            return None

        self.output_text.bind("<Key>", block_edit)

        y_scroll = ttk.Scrollbar(
            output_frame, orient=tk.VERTICAL, command=self.output_text.yview
        )
        x_scroll = ttk.Scrollbar(
            output_frame, orient=tk.HORIZONTAL, command=self.output_text.xview
        )

        self.output_text.configure(
            yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set
        )

        self.output_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        output_frame.grid_rowconfigure(0, weight=1)
        output_frame.grid_columnconfigure(0, weight=1)

        self.stdin_entry = None

        self.script_entry.bind("<Return>", lambda e: self._run_script())
        self.args_entry.bind("<Return>", lambda e: self._run_script())
        self.bind("<Control-f>", lambda e: self._show_search())

    def _get_output_revision(self) -> int:
        return self.output_revision

    def _toggle_advanced(self):
        """Toggle the advanced settings panel."""
        if self.advanced_expanded:
            self.advanced_frame.pack_forget()
            self.advanced_btn.configure(text="[>] Advanced")
            self.advanced_expanded = False
        else:
            self.advanced_frame.pack(fill=tk.X, pady=(0, 5), after=self.control_frame2)
            self.advanced_btn.configure(text="[v] Advanced")
            self.advanced_expanded = True

    def _show_search(self):
        """Show the search bar."""
        self.search_bar.show()

    def _browse_script(self):
        """Open file browser to select a script."""
        filepath = filedialog.askopenfilename(
            filetypes=[
                ("Python files", "*.py"),
                ("Shell scripts", "*.sh"),
                ("JavaScript files", "*.js"),
                ("All files", "*.*"),
            ]
        )
        if filepath:
            self.script_path_var.set(filepath)
            if not self.working_dir_var.get():
                self.working_dir_var.set(os.path.dirname(filepath))
            self.tab_name = os.path.basename(filepath)
            self.notebook.tab(self, text=self.tab_name)

            if self.auto_venv_var.get():
                self._auto_detect_venv(os.path.dirname(filepath))

    def _browse_working_dir(self):
        """Open directory browser to select working directory."""
        dirpath = filedialog.askdirectory()
        if dirpath:
            self.working_dir_var.set(dirpath)
            if self.auto_venv_var.get():
                self._auto_detect_venv(dirpath)

    def _browse_venv(self):
        """Open directory browser to select virtual environment."""
        dirpath = filedialog.askdirectory()
        if dirpath:
            self.venv_path_var.set(dirpath)

    def _auto_detect_venv(self, directory: str):
        """Auto-detect virtual environment in the given directory."""
        if directory in self._venv_cache:
            cached = self._venv_cache[directory]
            if cached:
                self.venv_path_var.set(cached)
            return

        detected_path = None
        for venv_name in [".venv", "venv", "env", ".env"]:
            venv_path = os.path.join(directory, venv_name)
            if os.path.isdir(venv_path):
                if os.path.isdir(os.path.join(venv_path, "Scripts")) or os.path.isdir(
                    os.path.join(venv_path, "bin")
                ):
                    detected_path = venv_path
                    break

        self._venv_cache[directory] = detected_path
        if detected_path:
            self.venv_path_var.set(detected_path)

    def _load_env_file(self):
        """Load environment variables from a .env file."""
        filepath = filedialog.askopenfilename(
            filetypes=[("Environment files", "*.env"), ("All files", "*.*")]
        )
        if filepath:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                self.env_text.delete("1.0", tk.END)
                self.env_text.insert("1.0", content)
            except Exception as e:
                show_custom_message(
                    self.winfo_toplevel(),
                    "Error",
                    f"Failed to load .env file: {e}",
                    "error",
                )

    def _parse_env_vars(self) -> Dict[str, str]:
        """Parse environment variables from the text area."""
        env_vars = {}
        content = self.env_text.get("1.0", tk.END).strip()
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                env_vars[key] = value
        return env_vars

    def _get_python_path(self) -> str:
        """Get the Python interpreter path, considering venv and platform."""
        venv_path = self.venv_path_var.get().strip()
        interpreter = self.interpreter_var.get()

        if venv_path and os.path.isdir(venv_path):
            if sys.platform == "win32":
                python_path = os.path.join(venv_path, "Scripts", "python.exe")
                if os.path.exists(python_path):
                    return python_path
            else:
                python_path = os.path.join(venv_path, "bin", interpreter)
                if os.path.exists(python_path):
                    return python_path
                for py in ["python3", "python"]:
                    python_path = os.path.join(venv_path, "bin", py)
                    if os.path.exists(python_path):
                        return python_path

        if interpreter in ["python", "python3"]:
            return get_default_python()

        return interpreter

    def _configure_run_buttons(self, running: bool):
        """Configure run/stop button states."""
        if running:
            self.run_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
        else:
            self.run_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)

    def _send_stdin(self, event=None):
        if self.stdin_entry is None:
            return
        txt = self.stdin_entry.get()
        if self.runner and txt:
            if self.runner.send_input(txt):
                self._append_output(f"> {txt}\n", "fg_90")
                self.stdin_entry.delete(0, tk.END)

    def _run_script(self):
        if self.is_running:
            return
        script_path = self.script_path_var.get().strip()
        if not script_path:
            return

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self._parse_env_vars())

        interpreter = self._get_python_path()
        cmd = [interpreter]
        if self.run_as_module_var.get():
            cmd.extend(["-u", "-m", script_path])
        else:
            cmd.extend(["-u", script_path])
        args = self.args_var.get().strip()
        if args:
            try:
                cmd.extend(shlex.split(args))
            except ValueError as exc:
                show_custom_message(
                    self.winfo_toplevel(),
                    "Invalid Arguments",
                    f"Could not parse arguments:\n{exc}",
                    "error",
                )
                return

        self.runner = ProcessRunner(
            cmd, Path(self.working_dir_var.get() or "."), env, self.output_queue
        )
        self._clear_output()
        self._append_output(f"--- Started: {datetime.now()} ---\n", "fg_36")

        self.is_running = True
        self.start_time = datetime.now()
        self._update_runtime()
        self._configure_run_buttons(running=True)
        self.status_var.set("Running...")
        self.runner.start()

    def _stop_script(self):
        if self.runner:
            self.runner.stop()
            self._append_output("\n[STOPPED] Prozessbaum beendet.\n", "fg_33")

    def _start_throttled_handler(self):
        def collector():
            while not self.stop_output_thread:
                try:
                    item = self.output_queue.get(timeout=0.1)
                    batch = [item]
                    start_gather = time.time()
                    while time.time() - start_gather < (UI_UPDATE_INTERVAL_MS / 1000):
                        try:
                            batch.append(self.output_queue.get_nowait())
                            if len(batch) >= 1000:
                                break
                        except queue.Empty:
                            break
                    self.after(0, self._process_batch_ui, batch)
                except queue.Empty:
                    continue
                except RuntimeError:
                    break

        threading.Thread(target=collector, daemon=True).start()

    def _get_line_tag(self, content: str) -> str:
        line_tag = "default"
        sample = content[:150]
        match = LOG_PATTERN.search(sample)
        if match:
            d = match.groupdict()
            if d.get("error"):
                line_tag = "fg_31"
            elif d.get("warning"):
                line_tag = "fg_33"
            elif d.get("info"):
                line_tag = "fg_36"
            elif d.get("success"):
                line_tag = "fg_32"
        return line_tag

    def _increment_output_counters(self, content: str):
        added_lines = content.count("\n")
        if added_lines:
            self.output_line_count += added_lines
            self.lines_since_trim_check += added_lines
        self.output_revision += 1

    def _process_batch_ui(self, batch):
        insert = self.output_text.insert
        pending_plain: List[Tuple[str, str]] = []

        def flush_pending_plain():
            if not pending_plain:
                return
            for tag, text_chunk in pending_plain:
                insert(tk.END, text_chunk, tag)
            pending_plain.clear()

        for msg_type, content in batch:
            if msg_type in {"output", "output_chunk", "tagged_output"}:
                tag = "default" if msg_type != "tagged_output" else content[1]
                text_chunk = content if msg_type != "tagged_output" else content[0]
                if msg_type != "tagged_output":
                    tag = self._get_line_tag(text_chunk)

                self._increment_output_counters(text_chunk)

                if "[" in text_chunk:
                    flush_pending_plain()
                    ANSIColorConverter.process_line(self.output_text, text_chunk)
                else:
                    if pending_plain and pending_plain[-1][0] == tag:
                        prev_tag, prev_text = pending_plain[-1]
                        pending_plain[-1] = (prev_tag, prev_text + text_chunk)
                    else:
                        pending_plain.append((tag, text_chunk))
            elif msg_type == "complete":
                flush_pending_plain()
                self._on_finished(
                    f"Finished (Exit: {content})", "fg_32" if content == 0 else "fg_31"
                )
            elif msg_type == "error":
                flush_pending_plain()
                self._on_finished(f"Error: {content}", "fg_31")

        flush_pending_plain()

        try:
            _, y_bottom = self.output_text.yview()
            if y_bottom > 0.9:
                self.output_text.see(tk.END)
        except tk.TclError:
            self.output_text.see(tk.END)

        self._trim_output()

    def _on_finished(self, msg, tag):
        self.is_running = False
        self._configure_run_buttons(running=False)
        self.status_var.set(msg)
        self._append_output(f"\n--- {msg} ---\n", tag)

    def _append_output(self, text: str, tag: str = "default"):
        """System-Helfer: Packt manuelle Meldungen in die Batch-Queue."""
        self.output_queue.put(("tagged_output", (text, tag)))

    def _on_script_complete(self, returncode: int):
        """Handle script completion."""
        self.is_running = False
        self._configure_run_buttons(running=False)

        elapsed = datetime.now() - self.start_time

        if returncode == 0:
            self.status_var.set(f"Completed successfully (exit code: {returncode})")
            self._append_output(
                f"\n[OK] Process completed successfully in {elapsed}\n", "fg_32"
            )
        else:
            self.status_var.set(f"Failed (exit code: {returncode})")
            self._append_output(
                f"\n[FAIL] Process failed with exit code {returncode} after {elapsed}\n",
                "fg_31",
            )

    def _on_script_error(self, error: str):
        """Handle script error."""
        self.is_running = False
        self._configure_run_buttons(running=False)
        self.status_var.set("Error")
        self._append_output(f"\n[ERROR] Error: {error}\n", "fg_31")

    def _clear_output(self):
        """Clear the output area."""
        self.output_text.delete("1.0", tk.END)
        self.output_line_count = 0
        self.lines_since_trim_check = 0
        self.output_revision += 1

    def _update_runtime(self):
        """Update the runtime display."""
        if self.is_running:
            elapsed = datetime.now() - self.start_time
            self.runtime_var.set(
                f"Runtime: {elapsed.seconds // 60}:{elapsed.seconds % 60:02d}"
            )
            self.after(1000, self._update_runtime)

    def _trim_output(self):
        """Kürzt das Log deterministisch anhand gepflegter Line-Counter."""
        if self.lines_since_trim_check < TRIM_CHECK_INTERVAL_LINES:
            return

        self.lines_since_trim_check = 0
        if self.output_line_count <= MAX_OUTPUT_LINES:
            return

        lines_to_remove = max(self.output_line_count - TARGET_OUTPUT_LINES, 0)
        if lines_to_remove <= 0:
            return

        self.output_text.delete("1.0", f"{lines_to_remove + 1}.0")
        self.output_line_count = max(self.output_line_count - lines_to_remove, 0)
        self.output_revision += 1

    def cleanup(self):
        """Clean up resources when tab is closed."""
        self.stop_output_thread = True
        if self.runner and self.is_running:
            self.runner.stop()

    def get_config(self) -> Dict[str, Any]:
        """Get the current tab configuration."""
        return {
            "tab_name": self.tab_name,
            "script_path": self.script_path_var.get(),
            "args": self.args_var.get(),
            "interpreter": self.interpreter_var.get(),
            "working_dir": self.working_dir_var.get(),
            "venv_path": self.venv_path_var.get(),
            "auto_venv": self.auto_venv_var.get(),
            "run_as_module": self.run_as_module_var.get(),
            "env_vars": self.env_text.get("1.0", tk.END).strip(),
        }

    def load_config(self, config: Dict[str, Any]):
        """Load configuration into the tab."""
        self.tab_name = config.get("tab_name", self.tab_name)
        self.script_path_var.set(config.get("script_path", ""))
        self.args_var.set(config.get("args", ""))
        self.interpreter_var.set(config.get("interpreter", "python"))
        self.working_dir_var.set(config.get("working_dir", ""))
        self.venv_path_var.set(config.get("venv_path", ""))
        self.auto_venv_var.set(config.get("auto_venv", True))
        self.run_as_module_var.set(config.get("run_as_module", False))
        self.env_text.delete("1.0", tk.END)
        self.env_text.insert("1.0", config.get("env_vars", ""))
        self.notebook.tab(self, text=self.tab_name)


# =============================================================================
# MAIN APPLICATION
# =============================================================================


class MiniTerminalPanel(ttk.Frame):
    """Shared app-wide mini terminal for an interactive manage.py shell."""

    POLL_INTERVAL_MS = 40
    MAX_BATCH_ITEMS = 200
    MAX_BATCH_CHARS = 32768
    DEFAULT_HEIGHT = 190

    def __init__(self, parent, app, **kwargs):
        super().__init__(parent, **kwargs)
        self.app = app
        self.output_queue = queue.SimpleQueue()
        self.runner: Optional[ProcessRunner] = None
        self.current_context: Optional[Dict[str, str]] = None
        self.script_path_var = tk.StringVar()
        self._setup_ui()
        self._schedule_poll()

    def _setup_ui(self):
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=6, pady=(6, 4))

        ttk.Label(header, text="Mini Terminal (manage.py shell)").pack(side=tk.LEFT)

        self.context_var = tk.StringVar(value="Context: not started")
        ttk.Label(header, textvariable=self.context_var).pack(side=tk.LEFT, padx=(12, 0))

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(header, textvariable=self.status_var).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Button(header, text="Start", command=self.start_shell, width=8).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(header, text="Stop", command=self.stop_shell, width=8).pack(side=tk.RIGHT)

        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        script_frame = ttk.Frame(body)
        script_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(script_frame, text="Script:").pack(side=tk.LEFT, padx=(0, 6))
        self.script_entry = ttk.Entry(script_frame, textvariable=self.script_path_var)
        self.script_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(script_frame, text="Browse...", command=self._browse_script, width=10).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(script_frame, text="Use Current Tab", command=self._use_current_tab_script).pack(side=tk.LEFT, padx=(6, 0))

        output_frame = ttk.Frame(body)
        output_frame.pack(fill=tk.BOTH, expand=True)

        self.output_text = tk.Text(
            output_frame,
            height=4,
            wrap=tk.WORD,
            background="#111827",
            foreground="#d1d5db",
            insertbackground="white",
            selectbackground="#3b82f6",
            selectforeground="white",
            inactiveselectbackground="#3b82f6",
            font=("Consolas", 9),
            undo=False,
            autoseparators=False,
            state=tk.NORMAL,
        )
        ANSIColorConverter.configure_tags(self.output_text)
        y_scroll = ttk.Scrollbar(output_frame, orient=tk.VERTICAL, command=self.output_text.yview)
        self.output_text.configure(yscrollcommand=y_scroll.set)
        self.output_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        output_frame.grid_rowconfigure(0, weight=1)
        output_frame.grid_columnconfigure(0, weight=1)

        input_frame = ttk.Frame(body)
        input_frame.pack(fill=tk.X, pady=(6, 0))

        ttk.Label(input_frame, text=">", font=("Consolas", 10)).pack(side=tk.LEFT, padx=(0, 5))
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(input_frame, textvariable=self.input_var, font=("Consolas", 10))
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_entry.bind("<Return>", self.send_input)
        ttk.Button(input_frame, text="Send", command=self.send_input, width=8).pack(side=tk.LEFT, padx=(6, 0))

    def _schedule_poll(self):
        self.after(self.POLL_INTERVAL_MS, self._poll_output)

    def _poll_output(self):
        try:
            pending_parts: List[str] = []
            items = 0
            chars = 0
            while items < self.MAX_BATCH_ITEMS and chars < self.MAX_BATCH_CHARS:
                try:
                    kind, payload = self.output_queue.get_nowait()
                except queue.Empty:
                    break

                items += 1
                if kind == "output_chunk":
                    pending_parts.append(payload)
                    chars += len(payload)
                elif kind == "complete":
                    self.status_var.set(f"Exited ({payload})")
                    self.runner = None
                elif kind == "error":
                    self.status_var.set("Error")
                    pending_parts.append(f"\n[mini-terminal error] {payload}\n")
                    self.runner = None

            if pending_parts:
                self._append_output("".join(pending_parts))
        finally:
            self._schedule_poll()

    def _append_output(self, text: str, tag: Optional[str] = None):
        if not text:
            return
        text_widget = self.output_text
        insert = text_widget.insert
        see = text_widget.see
        if "\x1b[" in text:
            ANSIColorConverter.process_line(text_widget, text)
        else:
            insert(tk.END, text, tag or "default")
        see(tk.END)

    def _browse_script(self):
        initial = self.script_path_var.get().strip()
        initial_dir = os.path.dirname(initial) if initial else os.getcwd()
        selected = filedialog.askopenfilename(
            title="Select script for mini terminal",
            initialdir=initial_dir,
            filetypes=[("Python files", "*.py"), ("All files", "*.*")],
        )
        if selected:
            self.script_path_var.set(selected)

    def _use_current_tab_script(self):
        current = self.app.notebook.select()
        if not current:
            return
        tab = self.app.notebook.nametowidget(current)
        if isinstance(tab, ScriptTab):
            script_path = tab.script_path_var.get().strip()
            if script_path:
                base_dir = tab.working_dir_var.get().strip()
                if not base_dir and getattr(tab, "workspace_dir", None):
                    base_dir = tab.workspace_dir
                if os.path.isabs(script_path):
                    resolved = script_path
                else:
                    resolved = os.path.join(base_dir or os.getcwd(), script_path)
                self.script_path_var.set(os.path.abspath(resolved))

    def _resolve_context(self) -> Optional[Dict[str, str]]:
        current = self.app.notebook.select()
        if not current:
            return None
        tab = self.app.notebook.nametowidget(current)
        if not isinstance(tab, ScriptTab):
            return None

        selected_script = self.script_path_var.get().strip()
        tab_script_path = tab.script_path_var.get().strip()
        tab_working_dir = tab.working_dir_var.get().strip()
        if not tab_working_dir and getattr(tab, "workspace_dir", None):
            tab_working_dir = tab.workspace_dir

        if selected_script:
            selected_script = os.path.abspath(selected_script)
        if tab_script_path:
            tab_script_path = os.path.abspath(tab_script_path) if os.path.isabs(tab_script_path) else os.path.abspath(os.path.join(tab_working_dir or os.getcwd(), tab_script_path))
        script_path = selected_script or tab_script_path
        script_dir = os.path.dirname(script_path) if script_path else ""
        working_dir = script_dir or tab_working_dir or os.getcwd()

        manage_path = script_path if script_path and os.path.basename(script_path).lower() == "manage.py" else os.path.join(working_dir, "manage.py")
        manage_path = os.path.abspath(manage_path)
        if not os.path.exists(manage_path):
            show_custom_message(
                self.app.root,
                "Mini Terminal",
                "Could not find manage.py for the mini terminal. Choose it explicitly or use the current tab script.",
                "warning",
            )
            return None

        self.script_path_var.set(manage_path)
        python_path = tab._get_python_path()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.update(tab._parse_env_vars())

        return {
            "manage_path": manage_path,
            "working_dir": working_dir,
            "python_path": python_path,
            "display": f"{os.path.basename(working_dir)} · {os.path.basename(manage_path)}",
            "env": env,
        }

    def start_shell(self):
        if self.runner:
            self.status_var.set("Already running")
            self.input_entry.focus_set()
            return

        context = self._resolve_context()
        if not context:
            return

        self.current_context = context
        self.output_text.delete("1.0", tk.END)
        self.context_var.set(f"Context: {context['display']}")
        self.status_var.set("Starting shell...")

        cmd = [context["python_path"], "-u", context["manage_path"], "shell"]
        self.runner = ProcessRunner(
            cmd, Path(context["working_dir"]), context["env"], self.output_queue
        )
        self.runner.start()
        self._append_output(f"[mini-terminal] Started: {' '.join(cmd)}\n", "fg_90")
        self.status_var.set("Running")
        self.input_entry.focus_set()

    def stop_shell(self):
        if self.runner:
            self.runner.stop()
            self.runner = None
            self.status_var.set("Stopped")
            self._append_output("\n[mini-terminal] Shell stopped.\n", "fg_90")

    def send_input(self, event=None):
        if not self.runner:
            self.start_shell()
            if not self.runner:
                return "break"

        text = self.input_var.get().rstrip()
        if not text:
            return "break"

        if self.runner.send_input(text):
            self._append_output(f">>> {text}\n", "fg_90")
            self.input_var.set("")
        else:
            self.status_var.set("Shell is not accepting input")
        return "break"




class ScriptRunnerApp:
    """Main application class for the Script Runner."""

    def __init__(self, root):
        self.root = root
        self.root.title("Script Runner")
        self.root.geometry("1300x850")
        self.root.minsize(900, 650)

        self.current_workspace = None
        self.has_unsaved_changes = False

        self._setup_styles()
        self._setup_menu()
        self._setup_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-f>", lambda e: self._search_current_tab())
        self.root.bind("<Control-j>", lambda e: self.toggle_mini_terminal())

    def _setup_styles(self):
        """Set up ttk styles for dark theme."""
        if HAS_BOOTSTRAP:
            return

        style = ttk.Style()
        available_themes = style.theme_names()
        if "clam" in available_themes:
            style.theme_use("clam")

        bg_color = "#1f2937"
        fg_color = "#f9fafb"
        accent_color = "#3b82f6"
        border_color = "#374151"
        hover_color = "#4b5563"

        style.configure(".", background=bg_color, foreground=fg_color)
        style.configure("TFrame", background=bg_color)
        style.configure("TLabel", background=bg_color, foreground=fg_color)
        style.configure("TNotebook", background=bg_color, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=bg_color,
            foreground=fg_color,
            padding=[15, 8],
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", accent_color), ("active", hover_color)],
            foreground=[("selected", "white")],
        )
        style.configure(
            "TEntry",
            fieldbackground="#374151",
            foreground=fg_color,
            bordercolor=border_color,
            lightcolor=border_color,
        )
        style.configure(
            "TButton",
            background=accent_color,
            foreground="white",
            padding=[10, 5],
            borderwidth=0,
        )
        style.map(
            "TButton",
            background=[("active", "#2563eb"), ("disabled", "#4b5563")],
            foreground=[("disabled", "#9ca3af")],
        )
        style.configure(
            "TCombobox",
            fieldbackground="#374151",
            foreground=fg_color,
            arrowcolor=fg_color,
        )
        style.configure("TCheckbutton", background=bg_color, foreground=fg_color)
        style.configure(
            "TScrollbar",
            background=border_color,
            troughcolor=bg_color,
            arrowcolor=fg_color,
        )
        self.root.configure(background=bg_color)

    def _setup_menu(self):
        """Set up the application menu."""
        menubar = tk.Menu(
            self.root,
            background="#1f2937",
            foreground="#f9fafb",
            activebackground="#3b82f6",
            activeforeground="white",
        )

        file_menu = tk.Menu(
            menubar,
            tearoff=0,
            background="#1f2937",
            foreground="#f9fafb",
            activebackground="#3b82f6",
            activeforeground="white",
        )
        file_menu.add_command(
            label="New Tab", command=self._add_tab, accelerator="Ctrl+T"
        )
        file_menu.add_command(
            label="Close Tab", command=self._close_current_tab, accelerator="Ctrl+W"
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Save Workspace", command=self._save_workspace, accelerator="Ctrl+S"
        )
        file_menu.add_command(
            label="Load Workspace", command=self._load_workspace, accelerator="Ctrl+O"
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Export Output...", command=self._export_output, accelerator="Ctrl+E"
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="Exit", command=self._on_close, accelerator="Alt+F4"
        )
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(
            menubar,
            tearoff=0,
            background="#1f2937",
            foreground="#f9fafb",
            activebackground="#3b82f6",
            activeforeground="white",
        )
        edit_menu.add_command(
            label="Find in Output",
            command=self._search_current_tab,
            accelerator="Ctrl+F",
        )
        edit_menu.add_command(
            label="Clear Output",
            command=self._clear_current_output,
            accelerator="Ctrl+L",
        )
        edit_menu.add_command(
            label="Copy Output", command=self._copy_output, accelerator="Ctrl+C"
        )
        menubar.add_cascade(label="Edit", menu=edit_menu)

        help_menu = tk.Menu(
            menubar,
            tearoff=0,
            background="#1f2937",
            foreground="#f9fafb",
            activebackground="#3b82f6",
            activeforeground="white",
        )
        help_menu.add_command(label="About", command=self._show_about)
        help_menu.add_command(label="Keyboard Shortcuts", command=self._show_shortcuts)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

        self.root.bind("<Control-t>", lambda e: self._add_tab())
        self.root.bind("<Control-w>", lambda e: self._close_current_tab())
        self.root.bind("<Control-l>", lambda e: self._clear_current_output())
        self.root.bind("<Control-s>", lambda e: self._save_workspace())
        self.root.bind("<Control-o>", lambda e: self._load_workspace())
        self.root.bind("<Control-e>", lambda e: self._export_output())

    def _setup_ui(self):
        """Set up the main user interface."""
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Toolbar
        toolbar = ttk.Frame(main_frame)
        toolbar.pack(fill=tk.X, pady=(0, 5))

        ttk.Button(toolbar, text="[>] Run All", command=self._run_all).pack(
            side=tk.LEFT, padx=(0, 5)
        )
        ttk.Button(toolbar, text="[ ] Stop All", command=self._stop_all).pack(
            side=tk.LEFT, padx=(0, 5)
        )
        ttk.Button(toolbar, text="+ New Tab", command=self._add_tab).pack(
            side=tk.LEFT, padx=(0, 20)
        )

        ttk.Button(
            toolbar, text="[S] Save Workspace", command=self._save_workspace
        ).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(
            toolbar, text="[L] Load Workspace", command=self._load_workspace
        ).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(toolbar, text="[E] Export Output", command=self._export_output).pack(
            side=tk.LEFT
        )
        ttk.Button(toolbar, text="[J] Mini Terminal", command=self.toggle_mini_terminal).pack(
            side=tk.RIGHT
        )

        self.vertical_pane = ttk.Panedwindow(main_frame, orient=tk.VERTICAL)
        self.vertical_pane.pack(fill=tk.BOTH, expand=True)

        self.notebook_container = ttk.Frame(self.vertical_pane)
        self.notebook = ttk.Notebook(self.notebook_container)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.vertical_pane.add(self.notebook_container, weight=5)

        self.mini_terminal = MiniTerminalPanel(self.vertical_pane, self)
        self.mini_terminal_visible = False

        self._add_tab()

    def _add_tab(self, config: Dict[str, Any] = None):
        tab = ScriptTab(self.notebook, self.notebook, self)
        self.notebook.add(tab, text=tab.tab_name)
        self.notebook.select(tab)
        if config:
            tab.load_config(config)
        self.has_unsaved_changes = True

    def toggle_mini_terminal(self):
        if self.mini_terminal_visible:
            self.vertical_pane.forget(self.mini_terminal)
            self.mini_terminal_visible = False
        else:
            self.vertical_pane.add(self.mini_terminal, weight=0)
            self.root.update_idletasks()
            try:
                total_height = max(1, self.vertical_pane.winfo_height() or self.root.winfo_height())
                initial_height = min(self.mini_terminal.DEFAULT_HEIGHT, max(120, total_height // 3))
                self.vertical_pane.sashpos(0, max(200, total_height - initial_height))
            except tk.TclError:
                pass
            self.mini_terminal_visible = True
            self.mini_terminal.input_entry.focus_set()
        return "break"

    def _close_current_tab(self):
        current = self.notebook.select()
        if current:
            if self.notebook.index("end") > 1:
                tab = self.notebook.nametowidget(current)
                if isinstance(tab, ScriptTab):
                    tab.cleanup()
                self.notebook.forget(current)
                self.has_unsaved_changes = True
            else:
                show_custom_message(
                    self.root, "Cannot Close", "Cannot close the last tab.", "info"
                )

    def _clear_current_output(self):
        current = self.notebook.select()
        if current:
            tab = self.notebook.nametowidget(current)
            if isinstance(tab, ScriptTab):
                tab._clear_output()

    def _copy_output(self):
        current = self.notebook.select()
        if current:
            tab = self.notebook.nametowidget(current)
            if isinstance(tab, ScriptTab):
                content = tab.output_text.get("1.0", tk.END)
                self.root.clipboard_clear()
                self.root.clipboard_append(content)

    def _search_current_tab(self):
        current = self.notebook.select()
        if current:
            tab = self.notebook.nametowidget(current)
            if isinstance(tab, ScriptTab):
                tab._show_search()

    def _export_output(self):
        current = self.notebook.select()
        if not current:
            return
        tab = self.notebook.nametowidget(current)
        if not isinstance(tab, ScriptTab):
            return
        content = tab.output_text.get("1.0", tk.END)
        if not content.strip():
            show_custom_message(self.root, "Export", "No output to export.", "info")
            return
        filepath = filedialog.asksaveasfilename(defaultextension=".txt")
        if filepath:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

    def _run_all(self):
        for tab_id in self.notebook.tabs():
            tab = self.notebook.nametowidget(tab_id)
            if isinstance(tab, ScriptTab) and not tab.is_running:
                tab._run_script()

    def _stop_all(self):
        for tab_id in self.notebook.tabs():
            tab = self.notebook.nametowidget(tab_id)
            if isinstance(tab, ScriptTab) and tab.is_running:
                tab._stop_script()

    def _save_workspace(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".json")
        if filepath:
            self._save_workspace_to_file(filepath)
            self.current_workspace = filepath
            self.has_unsaved_changes = False

    def _save_workspace_to_file(self, filepath: str):
        workspace = {
            "tabs": [
                self.notebook.nametowidget(t).get_config() for t in self.notebook.tabs()
            ]
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(workspace, f, indent=2)
        show_custom_message(
            self.root, "Saved", f"Workspace saved to:\n{filepath}", "info"
        )

    def _load_workspace(self):
        filepath = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if filepath:
            with open(filepath, "r", encoding="utf-8") as f:
                workspace = json.load(f)
            while self.notebook.index("end") > 0:
                tab_id = self.notebook.select()
                if tab_id:
                    self.notebook.nametowidget(tab_id).cleanup()
                    self.notebook.forget(tab_id)
                else:
                    break
            for tab_config in workspace.get("tabs", []):
                self._add_tab(tab_config)

    def _show_about(self):
        show_custom_message(
            self.root,
            "About Script Runner",
            "Script Runner v3.8.0\nHigh Performance Batch Logging & Auto-Colors.",
            "info",
        )

    def _show_shortcuts(self):
        show_custom_message(
            self.root,
            "Keyboard Shortcuts",
            "Ctrl+T: New Tab\nCtrl+W: Close Tab\nCtrl+S: Save Workspace\nCtrl+F: Find",
            "info",
        )

    def _on_close(self):
        if self.has_unsaved_changes:
            # Simple version for brevity in response, normally would be a choice dialog
            pass
        for tab_id in self.notebook.tabs():
            self.notebook.nametowidget(tab_id).cleanup()
        self.root.destroy()


def main():
    root = ttk.Window(themename="superhero") if HAS_BOOTSTRAP else tk.Tk()
    app = ScriptRunnerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
