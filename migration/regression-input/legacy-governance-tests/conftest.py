"""Governance-test Git fixture isolation.

Synthetic Git repositories are created by governance regression tests. On
macOS, Git automatic maintenance can briefly continue touching `.git` after an
assertion completes, racing `TemporaryDirectory` cleanup.

Apply the maintenance suppression only while tests under `tests/governance`
are executing. Explicit Git operations remain unchanged, and the environment
is restored after each governance test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_git_auto_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "gc.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "0")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "maintenance.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
