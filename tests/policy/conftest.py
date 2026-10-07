import os
import shutil

import pytest


@pytest.fixture(scope="session")
def gitleaks():
    """The gitleaks binary on PATH. Without it, `gitleaks` tests skip locally and fail in CI."""
    path = shutil.which("gitleaks")
    if path is None:
        if os.environ.get("CI"):
            pytest.fail("gitleaks is not on PATH; the CI job must install it")
        pytest.skip("gitleaks is not on PATH (brew install gitleaks)")
    return path
