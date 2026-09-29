import sys
from pathlib import Path

# In the images, shared/ modules are copied next to app/; mimic that for local tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
