#!/usr/bin/env python3
"""Launch the cc-mem web app backend.

    python3 manage.py                 # API on :8765, serves built UI if present
    python3 manage.py --port 9000

Dev:   run this, then in another terminal `cd ui && npm run dev` (Vite proxies
       /api here, hot-reload on http://localhost:5173).
Prod:  `cd ui && npm run build` once, then `python3 manage.py` serves the whole
       app on a single port.

Uses $CC_MEM_DB / $CC_MEM_EMBEDDER like the rest of cc-mem. To edit the real
semantic DB, launch with the venv python + CC_MEM_EMBEDDER=local so edits
re-embed in the same space.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webapp.server import serve


def main():
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--repo", help="inspect THIS repo's project memory "
                    "(default: the current directory). e.g. --repo ~/code/myapp")
    args = ap.parse_args()
    repo = args.repo or os.environ.get("CC_MEM_PROJECT_DIR")
    if repo:
        import webapp.server as s
        s.PROJECT_CWD = Path(repo).expanduser()
    serve(args.port)


if __name__ == "__main__":
    main()
