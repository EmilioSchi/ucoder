<div align="center">
  <a href="logo.png"><img src="logo.png" alt="ucoder logo" width="70" /></a>

$`\color{#4F84C2}\boldsymbol{\mu}`$**coder**<br><em>AI <strong>coding assistant</strong> with a command tool and file editing by line id.</em>

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-4F84C2" alt="license MIT" /></a>
  <a href="ucoder.py"><img src="https://img.shields.io/badge/python-3.9%2B-4F84C2" alt="python 3.9+" /></a>
  <a href="https://github.com/EmilioSchi/ucoder/releases"><img src="https://img.shields.io/badge/executable-all%20platforms-4F84C2" alt="executable for all platforms" /></a>
</p>
</div>

---

Minimal Claude Code alternative. Single Python file, zero dependencies, ~1200 lines.
Inspired from [nanocode](https://github.com/1rgs/nanocode) by [1rgs](https://github.com/1rgs).

<p align="center"><img src="screenshot.png" alt="ucoder" width="600" /></p>

## Tools

`bash` runs a command (`cmd.exe` on Windows), with an optional timeout, working directory, or background mode. `read` pages through a file; with `ids=true` each line gets a short stable id, and `replace`, `insert` and `delete` edit by those ids, so edits are exact without repeating the old text. `memory` keeps project notes in `.ucoder/ucoder.db`.

## Keys

| Key | Action |
|-----|--------|
| `Ctrl+C` | Interrupt the running turn; at the prompt, quit (as does `Ctrl+D`) |
| `Ctrl+G` | Open the current input in `$EDITOR` (vim by default); save and quit to send |
| `Ctrl+U` / `Ctrl+K` | Delete to start / end of line |
| `Ctrl+W` | Delete previous word |
| `Ctrl+A` / `Ctrl+E` | Jump to start / end of line |
| `Ctrl+R`, `↑` / `↓` | Search / browse input history, kept per project |

## Installation

**Single file.** Copy `ucoder.py` or download it, then run it with Python 3.9+ (no dependencies):

```sh
curl -O https://raw.githubusercontent.com/EmilioSchi/ucoder/main/ucoder.py
python3 ucoder.py
```

**Portable executable.** No Python needed: download the binary for your platform (Linux, macOS, Windows) from [Releases](https://github.com/EmilioSchi/ucoder/releases), then on Linux/macOS `chmod +x` it and run it.

## License

[MIT](LICENSE) © [EmilioSchi](https://github.com/EmilioSchi)
