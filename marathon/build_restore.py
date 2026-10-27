# --------- IMPORTS ---------
from pathlib import Path
import os
import sys
import PyInstaller.__main__


# --------- CONFIG ---------
app_name = "Restore Marathon Build"
app_version = "0.2.0"

project = Path(__file__).resolve().parents[1]
build_folder = project / "build"
cred_file = project / "marathon" / "cred.env"
blocked_tapes_file = project / "marathon" / "blocked_tapes.txt"


# --------- FUNC ---------
def _version_file(name: str, version: str, exe_name: str) -> Path:
    numbers = (tuple(int(part) for part in version.split(".")) + (0, 0, 0, 0))[:4]
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040704B0', [
          StringStruct('FileDescription', '{name}'), StringStruct('FileVersion', '{version}'),
          StringStruct('ProductName', '{name}'), StringStruct('ProductVersion', '{version}'),
          StringStruct('OriginalFilename', '{exe_name}.exe')])]),
        VarFileInfo([VarStruct('Translation', [1031, 1200])])])
"""
    path = build_folder / f"{exe_name}_version.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------- MAIN ---------
def main() -> None:
    if not cred_file.is_file():
        sys.exit(f"Zugangsdaten fehlen: {cred_file} mit ARK_HOST, ARK_USER, ARK_PASSWORD anlegen.")
    if not blocked_tapes_file.is_file():
        sys.exit(f"Sperrdatei fehlt: {blocked_tapes_file}; eine UTF-8-Datei mit einer Bandkennung je Zeile anlegen.")
    sys.path.insert(0, str(project))
    from marathon import restore_marathon
    exe_name = f"restore_marathon_{restore_marathon.app_version}"
    PyInstaller.__main__.run([
        str(project / "marathon" / "restore_marathon.py"), "--onefile", "--console", "--noconfirm",
        "--name", exe_name, "--paths", str(project),
        "--add-data", f"{cred_file}{os.pathsep}marathon",
        "--version-file", str(_version_file(restore_marathon.app_name, restore_marathon.app_version, exe_name)),
        "--distpath", str(project / "dist"), "--workpath", str(build_folder), "--specpath", str(build_folder),
    ])

    destination = project / "dist" / blocked_tapes_file.name
    try:
        with destination.open("xb") as handle:
            handle.write(blocked_tapes_file.read_bytes())
    except FileExistsError:
        pass  # Never replace an existing operational block list during a rebuild.


# --------- EXEC ---------
if __name__ == "__main__":
    main()
