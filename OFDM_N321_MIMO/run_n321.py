#!/usr/bin/python3
"""OFDM video over USRP N321, 2x2 MIMO loopback. See README.md."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
try:                                    # installed with `pip install -e .` (repo root)
    import ofdm321  # noqa: F401
except ImportError:                     # not installed: use the package next to this folder
    sys.path.insert(0, str(HERE.parent))
from ofdm321.config import set_app_root  # noqa: E402

set_app_root(HERE)                      # config/default.json and runs/ of THIS folder
from ofdm321.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
