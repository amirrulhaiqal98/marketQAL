"""
Pytest configuration for the Hermes test suite.

We enable ``asyncio_mode = "auto"`` so async test functions don't need
the ``@pytest.mark.asyncio`` decorator on every single test.
"""

from __future__ import annotations


# Pytest configuration values are read by pytest from module-level
# variables whose names start with ``pytest_``.
# See: https://docs.pytest.org/en/stable/reference/reference.html#confval-%C2%B7

pytest_plugins: list[str] = []  # reserved for future plugin entry-points


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Auto-mark async tests so we don't repeat @pytest.mark.asyncio."""
    import inspect

    for item in items:
        if inspect.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.asyncio)
