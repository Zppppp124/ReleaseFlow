from __future__ import annotations

import ctypes
import sys
import traceback

from releaseflow.ui import ReleaseFlowApp


def main() -> int:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    app = ReleaseFlowApp()
    try:
        app.mainloop()
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
