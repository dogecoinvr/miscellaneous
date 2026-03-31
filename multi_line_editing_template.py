"""
multiline_editor.py  –  Minimal viable demo template
=====================================================
A self-contained, inline multi-line terminal editor extracted from
llm_stream_with_tool.py.

Public API
----------
    text = edit(initial_text="", prompt=">> ")
        Returns the final text string, or None if the user cancelled (Ctrl+C / Ctrl+D).

    Keyboard shortcuts inside the editor
    ─────────────────────────────────────
    Arrow keys          Move cursor (wraps across lines)
    Home / End / Ctrl-A/E  Line start / end
    Enter               Insert newline
    Backspace / Delete  Delete character
    Ctrl-K              Kill to end of line
    Ctrl-U              Kill to start of line
    Mouse click         Move cursor to clicked position
    Ctrl-D or "." alone on a line  →  Submit
    Ctrl-C              Cancel (returns None)

Drop-in usage
-------------
    from multiline_editor import edit

    result = edit()
    if result is not None:
        print("You typed:", result)
"""

import os, select, sys

try:
    import tty as _tty, termios as _termios
    _HAS_RAW_TERM = True
except ImportError:
    _HAS_RAW_TERM = False

# ── Visual prefix drawn to the left of each line ──────────────────────────────
# The active line gets "> ", all others get "  ".  Two characters wide.
_EDIT_PREFIX  = "  "
_EDIT_PFX_LEN = len(_EDIT_PREFIX)

# ── Sentinel: typing this text alone on a line submits the buffer ──────────────
SUBMIT_SENTINEL = "."


# ══════════════════════════════════════════════════════════════════════════════
#  Core editor
# ══════════════════════════════════════════════════════════════════════════════

def edit(initial_text: str = "", prompt: str = ">> ") -> str | None:
    """
    Open the inline multi-line editor.

    Parameters
    ----------
    initial_text : str
        Pre-populate the buffer with this text.
    prompt : str
        A short hint printed above the editor area (not editable).

    Returns
    -------
    str   – the final text when the user submits.
    None  – if the user cancelled with Ctrl-C or Ctrl-D.
    """
    # ── Fallback: no raw-terminal support (e.g. Windows w/o pywin32) ──────────
    if not _HAS_RAW_TERM:
        return _edit_fallback(initial_text)

    # ── Buffer: list[list[str]], each inner list = one line's characters ───────
    if initial_text:
        buf: list[list[str]] = [list(ln) for ln in initial_text.split("\n")]
        row = len(buf) - 1
        col = len(buf[row])
    else:
        buf, row, col = [[]], 0, 0

    rendered    : int = 0   # total visual rows currently on screen
    cursor_vrow : int = 0   # visual row index of the cursor (0 = top of editor)
    edit_top_row: int = 1   # 1-indexed terminal row where the editor starts

    fd      = sys.stdin.fileno()
    old_cfg = _termios.tcgetattr(fd)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def tw() -> int:
        """Current terminal width in columns."""
        try:    return os.get_terminal_size().columns
        except: return 80

    def vis(chars: list[str], w: int | None = None) -> int:
        """Number of visual rows a line occupies given terminal width w."""
        if w is None: w = tw()
        return max(1, (_EDIT_PFX_LEN + len(chars) + w - 1) // w)

    def redraw() -> None:
        """Erase the editor area and repaint every line, then reposition the cursor."""
        nonlocal rendered, cursor_vrow
        w   = tw()
        out = []
        # Move up to the top of the editor area
        if cursor_vrow > 0:
            out.append(f"\x1b[{cursor_vrow}A")
        out.append("\x1b[1G\x1b[J")   # go to column 1, erase to end of screen

        new_h = 0
        for i, chars in enumerate(buf):
            ind = "> " if i == row else "  "
            out.append(f"{ind}{''.join(chars)}\r\n")
            new_h += vis(chars, w)

        rendered    = new_h
        tgt         = sum(vis(buf[r], w) for r in range(row)) + (_EDIT_PFX_LEN + col) // w
        cursor_vrow = tgt
        up          = rendered - tgt
        if up > 0:
            out.append(f"\x1b[{up}A")
        out.append(f"\x1b[{(_EDIT_PFX_LEN + col) % w + 1}G")

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def query_pos() -> int:
        """Ask the terminal for the cursor's current row (1-indexed)."""
        sys.stdout.write("\x1b[6n"); sys.stdout.flush()
        resp = b""
        while True:
            r, _, _ = select.select([fd], [], [], 0.3)
            if not r: break
            c = os.read(fd, 1); resp += c
            if c == b"R": break
        import re as _re
        m = _re.search(rb'\[(\d+);(\d+)R', resp)
        return int(m.group(1)) if m else edit_top_row

    def getch() -> str:
        """
        Read one logical keypress from stdin (raw mode).
        Returns a string: printable char, control char, or an escape sequence
        like "\x1b[A" (arrow-up), "HOME", "END", "DEL", or "\x00M:…" (mouse).
        """
        b     = os.read(fd, 1)
        if not b: return ""
        first = b[0]
        # Multi-byte UTF-8
        if   first >= 0xF0: b += os.read(fd, 3)
        elif first >= 0xE0: b += os.read(fd, 2)
        elif first >= 0xC0: b += os.read(fd, 1)
        ch = b.decode("utf-8", errors="replace")

        if ch != "\x1b":
            return ch

        # Escape sequence handling
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r: return ch
        ch2 = os.read(fd, 1).decode("utf-8", errors="replace")

        # Application cursor key mode: ESC O {A/B/C/D/H/F} → normalise to CSI
        if ch2 == "O":
            r2, _, _ = select.select([fd], [], [], 0.05)
            if r2:
                ch3 = os.read(fd, 1).decode("utf-8", errors="replace")
                if ch3 in "ABCDHF": return "\x1b[" + ch3
            return "\x1bO"

        if ch2 != "[": return ch + ch2

        r2, _, _ = select.select([fd], [], [], 0.05)
        if not r2: return ch + ch2
        ch3 = os.read(fd, 1).decode("utf-8", errors="replace")

        if ch3 in "ABCDHF": return "\x1b[" + ch3

        # SGR mouse: ESC [ < btn ; col ; row M/m
        if ch3 == "<":
            seq = ""
            while True:
                r3, _, _ = select.select([fd], [], [], 0.1)
                if not r3: break
                c = os.read(fd, 1).decode("utf-8", errors="replace")
                seq += c
                if c in "Mm": break
            return "\x00M:" + seq

        # ESC [ {1/3/4} ~ → HOME / DEL / END
        if ch3 in "134":
            select.select([fd], [], [], 0.05); os.read(fd, 1)   # consume '~'
            return {"1": "HOME", "3": "DEL", "4": "END"}[ch3]

        return "\x1b[" + ch3

    def run_instant(fn) -> None:
        """
        Temporarily leave raw mode, run fn() (which may print to stdout),
        then re-enter raw mode and re-anchor + redraw the editor.
        """
        nonlocal rendered, edit_top_row
        if cursor_vrow > 0:
            sys.stdout.write(f"\x1b[{cursor_vrow}A")
        sys.stdout.write("\x1b[1G\x1b[J"); sys.stdout.flush()
        rendered = 0
        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_cfg)
        try:   fn()
        finally: _tty.setraw(fd)
        edit_top_row = query_pos()
        redraw()

    # ── Print the hint line before entering raw mode ───────────────────────────
    if prompt:
        print(prompt)
        print(f"  (type '{SUBMIT_SENTINEL}' alone on a line to submit, Ctrl-C to cancel)\n")

    # ── Main event loop ────────────────────────────────────────────────────────
    try:
        _tty.setraw(fd)
        sys.stdout.write("\x1b[?1000h\x1b[?1006h"); sys.stdout.flush()  # enable SGR mouse
        edit_top_row = query_pos()
        redraw()

        while True:
            key = getch()

            # ── Cancel ────────────────────────────────────────────────────────
            if key == "\x03":   # Ctrl-C
                return None
            if key == "\x04":   # Ctrl-D  (treat as submit if buffer non-empty, else cancel)
                text = "\n".join("".join(ln) for ln in buf).strip()
                return text if text else None

            # ── Mouse click ───────────────────────────────────────────────────
            elif key.startswith("\x00M:"):
                seq = key[3:]
                if seq.endswith("M"):   # press only (ignore release "m")
                    parts = seq[:-1].split(";")
                    if len(parts) == 3:
                        btn_n = int(parts[0])
                        t_col = int(parts[1]) - 1
                        t_row = int(parts[2]) - 1
                        if btn_n in (0, 1, 2):
                            rel = t_row - (edit_top_row - 1)
                            if 0 <= rel < rendered:
                                w    = tw()
                                vrow = 0
                                for i, chars in enumerate(buf):
                                    vh = vis(chars, w)
                                    if vrow + vh > rel:
                                        row      = i
                                        line_vr  = rel - vrow
                                        char_idx = line_vr * w + t_col
                                        col      = max(0, min(char_idx - _EDIT_PFX_LEN, len(chars)))
                                        break
                                    vrow += vh
                                redraw()

            # ── Enter ─────────────────────────────────────────────────────────
            elif key in ("\r", "\n"):
                line_text = "".join(buf[row]).strip()
                if line_text == SUBMIT_SENTINEL:
                    # Remove the sentinel line and return
                    buf.pop(row)
                    text = "\n".join("".join(ln) for ln in buf)
                    return text
                # Normal newline: split current line at cursor
                rest     = buf[row][col:]
                buf[row] = buf[row][:col]
                row += 1
                buf.insert(row, list(rest))
                col = 0
                redraw()

            # ── Backspace ─────────────────────────────────────────────────────
            elif key in ("\x7f", "\x08"):
                if col > 0:
                    buf[row].pop(col - 1); col -= 1
                elif row > 0:
                    prev = len(buf[row - 1])
                    buf[row - 1].extend(buf[row]); buf.pop(row)
                    row -= 1; col = prev
                redraw()

            # ── Delete ────────────────────────────────────────────────────────
            elif key == "DEL":
                if   col < len(buf[row]):  buf[row].pop(col)
                elif row < len(buf) - 1:   buf[row].extend(buf[row + 1]); buf.pop(row + 1)
                redraw()

            # ── Arrow keys ────────────────────────────────────────────────────
            elif key == "\x1b[A":   # Up
                if row > 0:          row -= 1; col = min(col, len(buf[row])); redraw()
            elif key == "\x1b[B":   # Down
                if row < len(buf)-1: row += 1; col = min(col, len(buf[row])); redraw()
            elif key == "\x1b[C":   # Right
                if   col < len(buf[row]): col += 1
                elif row < len(buf)-1:    row += 1; col = 0
                redraw()
            elif key == "\x1b[D":   # Left
                if   col > 0: col -= 1
                elif row > 0: row -= 1; col = len(buf[row])
                redraw()

            # ── Home / End ────────────────────────────────────────────────────
            elif key in ("\x1b[H", "HOME", "\x01"):   # Ctrl-A
                col = 0; redraw()
            elif key in ("\x1b[F", "END",  "\x05"):   # Ctrl-E
                col = len(buf[row]); redraw()

            # ── Kill shortcuts ────────────────────────────────────────────────
            elif key == "\x0b":   # Ctrl-K: kill to end of line
                buf[row] = buf[row][:col]; redraw()
            elif key == "\x15":   # Ctrl-U: kill to start of line
                buf[row] = buf[row][col:]; col = 0; redraw()

            # ── Printable character ───────────────────────────────────────────
            elif len(key) >= 1 and (len(key) > 1 or ord(key) >= 32):
                for ch in key:
                    buf[row].insert(col, ch); col += 1
                redraw()

    finally:
        sys.stdout.write("\x1b[?1000l\x1b[?1006l"); sys.stdout.flush()  # disable mouse
        _termios.tcsetattr(fd, _termios.TCSADRAIN, old_cfg)
        sys.stdout.write("\r\n"); sys.stdout.flush()


# ── Non-raw fallback (Windows / pipe / no tty) ─────────────────────────────────
def _edit_fallback(initial_text: str = "") -> str | None:
    """Line-by-line input loop when raw terminal is unavailable."""
    buf = list(initial_text.split("\n")) if initial_text else []
    print(f"(multi-line mode — type '{SUBMIT_SENTINEL}' on its own line to submit)")
    try:
        while True:
            line = input()
            if line.strip() == SUBMIT_SENTINEL:
                return "\n".join(buf)
            buf.append(line)
    except (EOFError, KeyboardInterrupt):
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Demo
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== multiline_editor demo ===")
    print("Edit freely. Arrow keys, Home/End, Backspace, Delete all work.")
    print(f"Type '{SUBMIT_SENTINEL}' alone on a line (or Ctrl-D) to submit.")
    print("Ctrl-C to cancel.\n")

    result = edit(prompt="Enter your text:")

    if result is None:
        print("(cancelled)")
    else:
        print("\n── You submitted ──────────────────")
        print(result)
        print("───────────────────────────────────")
        print(f"({len(result.splitlines())} lines, {len(result)} chars)")
