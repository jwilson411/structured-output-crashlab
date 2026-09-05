import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# Works whether or not the package has been pip-installed into the environment.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CASES_ROOT = REPO_ROOT / "cases"


@pytest.fixture(scope="session")
def cases_root() -> Path:
    return CASES_ROOT
