"""KIC SignalForge - 개발/실행용 런처.

    python run.py            # http://127.0.0.1:8000
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
