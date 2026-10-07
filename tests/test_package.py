"""The package itself: one version, a typed surface, every exported name real."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import eifi1_server_kit

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_is_stated_once() -> None:
    """``pyproject.toml`` and ``__version__`` agree, so a tag ``v<version>`` names one thing."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == eifi1_server_kit.__version__
    assert f"## [{eifi1_server_kit.__version__}]" in (ROOT / "CHANGELOG.md").read_text()


def test_the_package_is_typed() -> None:
    assert (Path(eifi1_server_kit.__file__).parent / "py.typed").is_file()


def test_every_exported_name_exists() -> None:
    for module_name in (
        "eifi1_server_kit.auth",
        "eifi1_server_kit.feedback",
        "eifi1_server_kit.mail",
        "eifi1_server_kit.settings",
        "eifi1_server_kit.translation_review",
        "eifi1_server_kit.user_admin",
    ):
        module = importlib.import_module(module_name)
        exported = module.__all__
        assert len(exported) == len(set(exported)), module_name
        for name in exported:
            assert hasattr(module, name), f"{module_name}.{name}"
