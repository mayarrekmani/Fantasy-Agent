#!/usr/bin/env python3
"""Regenerate public/app.css from the engine's stylesheet. Run this after changing CSS in fantasy.py:

    python scripts/build_css.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fantasy  # noqa: E402

out = os.path.join(ROOT, "public", "app.css")
with open(out, "w", encoding="utf-8") as f:
    f.write(fantasy.CSS.strip() + "\n")
print(f"wrote {out}")
