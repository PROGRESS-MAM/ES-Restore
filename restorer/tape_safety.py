# --------- IMPORTS ---------
from datetime import datetime
from pathlib import Path
import os
import re


# --------- CONFIG ---------
app_name = "Restore Tape Safety"
app_version = "0.1.0"


# --------- FUNC ---------
class _TapeSafety:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.pending: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _key(tape: str) -> str:
        if not isinstance(tape, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", tape):
            raise ValueError(f"Ungültige Bandkennung {tape!r}; eine Kennung ohne Leerzeichen verwenden.")
        return tape.casefold()

    def _read_file(self) -> set[str]:
        text = self.path.read_text(encoding="utf-8-sig")
        blocked = set()
        for number, line in enumerate(text.splitlines(), 1):
            tape = line.partition("#")[0].strip()
            if not tape:
                continue
            try:
                blocked.add(self._key(tape))
            except ValueError as exc:
                raise ValueError(f"Sperrdatei {self.path}, Zeile {number}: {exc}") from exc
        return blocked

    def read(self) -> set[str]:
        if self.path is None:
            return set(self.pending)
        blocked = self._read_file()
        missing = [entry for key, entry in self.pending.items() if key not in blocked]
        if missing:
            # Append instead of replacing the file to preserve manual edits and comments.
            with self.path.open("r+b") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                if end:
                    handle.seek(-1, os.SEEK_END)
                    prefix = "" if handle.read(1) == b"\n" else "\n"
                else:
                    prefix = ""
                handle.seek(0, os.SEEK_END)
                handle.write((prefix + "".join(f"{tape} # {note}\n" for tape, note in missing)).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            blocked = self._read_file()
            if any(key not in blocked for key in self.pending):
                raise OSError(f"Sperrdatei {self.path} wurde gleichzeitig geändert; Speicherung wird erneut versucht.")
        self.pending.clear()
        return blocked

    def block(self, tape: str, reason: str) -> bool:
        key = self._key(tape)
        already_pending = key in self.pending
        date = datetime.now().astimezone().isoformat(timespec="seconds")
        note = f"auto {date}: {' '.join(reason.split())}"
        self.pending.setdefault(key, (tape, note))
        blocked = self._read_file() if self.path is not None else set()
        if key in blocked:
            self.pending.pop(key, None)
            return False
        self.read()
        return not already_pending


# --------- MAIN ---------
def main() -> None:
    """This private helper is used by the Restorer, not as a standalone command."""
    raise SystemExit("Dieses Modul wird vom Restorer verwendet.")


# --------- EXEC ---------
if __name__ == "__main__":
    main()
