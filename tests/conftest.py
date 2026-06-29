import os
import sys

# Ensure project root is on import path when running pytest from any directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
