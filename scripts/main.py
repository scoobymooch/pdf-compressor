#!/usr/bin/env python3
"""compress-pdf — unified entry point.

No arguments → launches the macOS GUI.
Any arguments → runs the CLI (same flags as compress_pdf.py).
"""

import sys


def main():
    if len(sys.argv) > 1:
        from compress_pdf import main as cli_main
        cli_main()
    else:
        from compress_pdf_gui import App
        App()


if __name__ == "__main__":
    main()
