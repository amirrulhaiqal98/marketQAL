"""
Pytest configuration for the Hermes test suite.

Async tests are auto-marked by ``asyncio_mode = "auto"`` in
``pyproject.toml`` (pytest-asyncio >= 0.23), so individual test files
do **not** need ``@pytest.mark.asyncio`` decorators.

This conftest exists so future shared fixtures (e.g. sample payloads,
in-memory click streams) have a single canonical home. Add fixtures
here rather than redefining them per-test file.
"""

from __future__ import annotations


pytest_plugins: list[str] = []  # reserved for future plugin entry-points

