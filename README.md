# Script Runner Terminal Emulator

A desktop GUI for running scripts in separate tabs and viewing live terminal output. It is built with Python and Tkinter, with optional `ttkbootstrap` styling.

Made this mainly for a simple way to start django projects.

## Features

- Run multiple scripts in separate tabs
- Live stdout/stderr output in a terminal-style text view
- ANSI color support for colored command output
- Automatic keyword highlighting for common log levels such as `INFO`, `WARNING`, `ERROR`, `SUCCESS`, and `Traceback`
- Script arguments, custom working directory, and interpreter selection
- Virtual environment selection with auto-detection for `.venv`, `venv`, `env`, and `.env` folders
- Per-tab environment variables, including loading from `.env` files
- Save and load workspaces as JSON
- Search within output with `Ctrl+F`
- Export output to a text file
- Run all / stop all controls
- App-wide mini terminal for a Django `manage.py shell`
- Process-tree stopping via `psutil`
- Batched output handling for better performance with high-volume logs

## Requirements

- Python 3.8 or newer recommended
- Tkinter, usually included with standard Python installations
- `psutil`
- `ttkbootstrap` optional, used for a modern theme when installed

Install the Python dependencies:

```bash
pip install psutil ttkbootstrap
```

`ttkbootstrap` is optional. If it is not installed, the app falls back to the standard Tkinter `ttk` widgets.

## Usage

Run the app with:

```bash
python script_runner_terminal_emulator.py
```

or:

```bash
python3 script_runner_terminal_emulator.py
```

## Running a script

1. Click **Browse** and select a script.
2. Add any command-line arguments in the **Arguments** field.
3. Choose an interpreter such as `currently just python, might add more`.
4. Set a working directory if needed.
5. Click **Run**.

The script output appears in the tab output panel. Use **Stop** to terminate the running process and its child processes.

## Advanced options

Click **Advanced** to configure:

- A virtual environment path
- Automatic virtual environment detection
- Running Python code with `-m`
- Environment variables in `KEY=VALUE` format
- Loading variables from a `.env` file

Example environment variable format:

```env
DEBUG=true
DJANGO_SETTINGS_MODULE=myproject.settings
API_BASE_URL=http://localhost:8000
```

## Workspaces

Use **Save Workspace** to save the current tab setup to a JSON file. A workspace can include:

- Script paths
- Arguments
- Interpreter settings
- Working directories
- Virtual environment paths
- Environment variables

Use **Load Workspace** to restore a saved setup.

## Mini terminal

The **Mini Terminal** panel is designed for launching a Django `manage.py shell` using the context from the current tab.

You can:

- Select a `manage.py` file manually
- Use the script from the current tab
- Start and stop the shell
- Send commands through the input field

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+T` | New tab |
| `Ctrl+W` | Close current tab |
| `Ctrl+S` | Save workspace |
| `Ctrl+O` | Load workspace |
| `Ctrl+F` | Find in output |
| `Ctrl+L` | Clear output |
| `Ctrl+E` | Export output |
