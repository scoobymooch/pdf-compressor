"""PyInstaller hook for Pillow (PIL).

Pillow wheels ship with bundled dylibs in PIL/.dylibs/ (placed by delocate).
"""
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

binaries = collect_dynamic_libs("PIL")
datas = collect_data_files("PIL")
