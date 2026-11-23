# PyInstaller spec for Genesis (one-folder build, windowed).
# Build with:  python -m PyInstaller --noconfirm --clean scripts\genesis.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller

datas = [(str(ROOT / "genesis" / "nlp" / "data"), "genesis/nlp/data"), (str(ROOT / ".env.example"), ".")]
binaries = []
hiddenimports = collect_submodules("genesis") + ["sklearn.utils._typedefs", "sklearn.neighbors._partition_nodes",
                                                   "win32com.client", "pythoncom", "keyring.backends.Windows"]
for pkg in ("vosk", "ctranslate2", "faster_whisper"):
    try:
        binaries += collect_dynamic_libs(pkg)
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(  # noqa: F821
    [str(ROOT / "genesis" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "torch", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
icon = ROOT / "assets" / "genesis.ico"
exe = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True, name="Genesis", console=False,
    icon=str(icon) if icon.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Genesis")  # noqa: F821
