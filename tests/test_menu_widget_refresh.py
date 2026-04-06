from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_dashboard_refresh_fetches_visible_menu_leaves():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    result = subprocess.run(
        [node, "--test", str(REPO_ROOT / "tests" / "menu_widget_refresh.test.js")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        "Node menu widget test failed.\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
