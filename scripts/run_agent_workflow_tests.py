#!/usr/bin/env python3
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
os.execvp(
    "python3",
    ["python3", "-m", "unittest", "discover", "-s", "scripts/tests", "-p", "test_*.py"],
)
