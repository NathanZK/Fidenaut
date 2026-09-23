#!/usr/bin/env python3
"""Run the checked-in Ruff lint/static-analysis check for provider Python code."""

import os
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    os.chdir(ROOT)
    os.execvp("python3", ["python3", "-m", "ruff", "check", "scripts"])


if __name__ == "__main__":
    sys.exit(main())
