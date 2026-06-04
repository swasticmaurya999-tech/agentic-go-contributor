import sys

# Make stdout/stderr tolerant of non-ASCII (Windows defaults to cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from .cli import main

if __name__ == "__main__":
    main()
