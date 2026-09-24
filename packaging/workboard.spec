# PyInstaller spec for the self-contained WorkBoard release binaries.
# Build through `python packaging/build.py binary`, which also archives the result.
#
# One onedir app: console `workboard` (plus windowed `workboardw` on Windows, used
# by the background service) sharing one `_internal/`. Package data keeps its
# package-relative paths so importlib.resources.files("workboard") finds it frozen.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))  # collect_submodules imports from this sys.path

PACKAGE_DATA = ("web/board.html", "skills/workboard/SKILL.md")

a = Analysis(
    [str(Path(SPECPATH) / "entry.py")],
    pathex=[str(SRC)],
    hiddenimports=collect_submodules("workboard"),
    datas=[(str(SRC / "workboard" / rel), str(Path("workboard", rel).parent)) for rel in PACKAGE_DATA],
)
pyz = PYZ(a.pure)


def executable(name, console):
    return EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=console,
               upx=False, strip=False)


executables = [executable("workboard", console=True)]
if sys.platform == "win32":
    executables.append(executable("workboardw", console=False))

COLLECT(*executables, a.binaries, a.datas, name="workboard", upx=False, strip=False)
