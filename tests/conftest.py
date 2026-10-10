import sys
from pathlib import Path

import pytest

# Tests import the app the way it runs: from the project root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def fresh_job_manager():
    """JobManager is a process-wide singleton; give every test its own."""
    from src.job_manager import JobManager
    JobManager._instance = None
    yield
    JobManager._instance = None
