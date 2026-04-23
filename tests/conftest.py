"""
Test fixtures. The Lambda source sits at the repo root, not in a package, so
put the repo root on sys.path before anything imports lambda_function.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
