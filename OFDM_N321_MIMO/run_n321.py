#!/usr/bin/python3
"""OFDM video over USRP N321, 2x2 MIMO loopback. See README.md."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
try:                                    # installed with `pip install -e .` (repo root)
    import ofdm321  # noqa: F401
except ImportError:                     # not installed: use the package next to this folder
    sys.path.insert(0, str(HERE.parent))
    import ofdm321  # noqa: F401
if getattr(ofdm321, "__file__", None) is None:
    # a namespace package: an old ofdm321/ folder (left with only __pycache__ after the move to the repo root)
    # shadows the real package
    stale = [str(p) for p in getattr(ofdm321, "__path__", [])]
    sys.stderr.write("LỖI: 'import ofdm321' tìm thấy thư mục cũ không có __init__.py (chỉ còn __pycache__):\n  "
                     + "\n  ".join(stale) + "\nXóa nó rồi chạy lại:\n  rm -rf " + " ".join(f"'{p}'" for p in stale)
                     + "\n(gói thật ở " + str(HERE.parent / "ofdm321") + "; cài: /usr/bin/python3 -m pip install "
                     "--user --break-system-packages --no-build-isolation -e " + str(HERE.parent) + ")\n")
    raise SystemExit(2)
from ofdm321.config import set_app_root  # noqa: E402

set_app_root(HERE)                      # config/default.json and runs/ of THIS folder
from ofdm321.runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
