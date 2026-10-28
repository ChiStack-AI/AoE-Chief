"""Fresh module loading for approval/reload acceptance tests (no cached globals)."""
import importlib.util
import os
from pathlib import Path
from unittest import mock


def load_source(path, name, state_home):
    """Execute actual source, bypassing timestamp/size-based .pyc reuse.

    Each call models a new process's loaded code and startup hashes, while
    sharing only the real on-disk Chief state supplied by the test.
    """
    path = Path(path)
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"AOE_CHIEF_HOME": str(state_home),
                                     "AOE_CHIEF_LANG": "en"}):
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module
