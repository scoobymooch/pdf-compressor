"""PyInstaller hook for pikepdf.

pikepdf wheels ship with bundled dylibs in the .dylibs/ subdirectory
(placed there by delocate). We need to collect those libraries and
the package's data files.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Collect bundled dylibs from pikepdf/.dylibs/
binaries = collect_dynamic_libs("pikepdf")

# Collect any non-Python data files (codec resources, etc.)
datas = collect_data_files("pikepdf")
