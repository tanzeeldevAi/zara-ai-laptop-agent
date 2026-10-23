import sys
from pathlib import Path

# Make the repo root importable (`agent` package) no matter where pytest runs from.
sys.path.insert(0, str(Path(__file__).resolve().parent))
