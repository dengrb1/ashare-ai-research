# PyInstaller spec for the standalone ashare-ai CLI.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).resolve().parent
source_root = project_root / "src"

datas = [
    (str(project_root / "alembic.ini"), "."),
    (str(project_root / "requirements.lock"), "."),
    (str(project_root / "migrations"), "migrations"),
    (str(project_root / "configs"), "configs"),
    (
        str(project_root / "src" / "ashare_ai" / "reports" / "templates"),
        "ashare_ai/reports/templates",
    ),
]
datas.extend(collect_data_files("ashare_ai"))

# The API and migration entry points are loaded by name at runtime. Collecting
# the package submodules keeps all CLI commands available in the one-file build.
hiddenimports = sorted(
    {
        *collect_submodules("ashare_ai"),
        "alembic.command",
        "alembic.config",
        "alembic.context",
        "alembic.environment",
        "alembic.migration",
        "uvicorn.lifespan.on",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
    }
)

a = Analysis(
    [str(source_root / "ashare_ai" / "cli.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ashare-ai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
