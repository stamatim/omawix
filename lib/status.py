#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from omawix_library import collect_library


def main() -> int:
    try:
        status = collect_library()
        if len(sys.argv) > 1:
            limit = max(1, min(100, int(sys.argv[1])))
            status["books"] = status["books"][:limit]
        print(json.dumps(status, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
