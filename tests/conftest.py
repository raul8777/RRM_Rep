import os
import sys

# Ensure project root is on import path when running pytest from any directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def pytest_addoption(parser):
    parser.addoption(
        "--mimic-update-baseline", action="store_true", default=False,
        help="Update MIMIC regression baseline instead of comparing",
    )
