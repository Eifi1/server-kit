"""The package itself: one version, a typed surface, every exported name real."""

from __future__ import annotations

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

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
        "eifi1_server_kit.billing",
        "eifi1_server_kit.billing.testing",
        "eifi1_server_kit.clock",
        "eifi1_server_kit.cors",
        "eifi1_server_kit.demo",
        "eifi1_server_kit.dev_switch",
        "eifi1_server_kit.errors",
        "eifi1_server_kit.feedback",
        "eifi1_server_kit.feedback.router",
        "eifi1_server_kit.feedback.testing",
        "eifi1_server_kit.jobs",
        "eifi1_server_kit.limiter",
        "eifi1_server_kit.mail",
        "eifi1_server_kit.notifications",
        "eifi1_server_kit.settings",
        "eifi1_server_kit.tours",
        "eifi1_server_kit.tours.router",
        "eifi1_server_kit.tours.testing",
        "eifi1_server_kit.translation_review",
        "eifi1_server_kit.translation_review.router",
        "eifi1_server_kit.translation_review.testing",
        "eifi1_server_kit.uploads",
        "eifi1_server_kit.user_admin",
    ):
        module = importlib.import_module(module_name)
        exported = module.__all__
        assert len(exported) == len(set(exported)), module_name
        for name in exported:
            assert hasattr(module, name), f"{module_name}.{name}"


def test_feedback_layer_2_loads_no_web_framework() -> None:
    """The ports, the service and ``feedback.testing`` run without FastAPI or httpx — in a
    CLI such as Kurvenschmiede's ``feedback_loop.py`` (server-services §2.1). A fresh
    interpreter, so nothing another test imported counts."""
    probe = (
        "import sys, eifi1_server_kit.feedback, eifi1_server_kit.feedback.testing;"
        "print(sorted({name.split('.')[0] for name in sys.modules} & {'fastapi', 'httpx', 'sqlalchemy'}))"
    )
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert loaded.strip() == "[]"


def test_the_feedback_router_names_its_extra_without_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import fastapi`` fail as if it were not installed."""
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.feedback.router", raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    with pytest.raises(ImportError, match=r"install the kit with its fastapi extra, eifi1-server-kit\[fastapi\]"):
        importlib.import_module("eifi1_server_kit.feedback.router")


def test_translation_review_layer_2_loads_no_web_framework() -> None:
    """The reviewer view, the store port, the service and ``translation_review.testing`` run
    without FastAPI, httpx or SQLAlchemy (server-services §6.2.1). A fresh interpreter."""
    probe = (
        "import sys, eifi1_server_kit.translation_review, eifi1_server_kit.translation_review.testing;"
        "print(sorted({name.split('.')[0] for name in sys.modules} & {'fastapi', 'httpx', 'sqlalchemy'}))"
    )
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert loaded.strip() == "[]"


def test_the_translation_review_router_names_its_extra_without_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.translation_review.router", raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    with pytest.raises(
        ImportError,
        match=r"translation_review\.router needs FastAPI: install the kit with its fastapi extra, "
        r"eifi1-server-kit\[fastapi\]",
    ):
        importlib.import_module("eifi1_server_kit.translation_review.router")


def test_tours_layer_2_loads_no_web_framework() -> None:
    """The store port, the service and ``tours.testing`` run without FastAPI, httpx or
    SQLAlchemy (tours §10.3). A fresh interpreter."""
    probe = (
        "import sys, eifi1_server_kit.tours, eifi1_server_kit.tours.testing;"
        "print(sorted({name.split('.')[0] for name in sys.modules} & {'fastapi', 'httpx', 'sqlalchemy'}))"
    )
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert loaded.strip() == "[]"


def test_the_tours_router_names_its_extra_without_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.tours.router", raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    with pytest.raises(
        ImportError,
        match=r"tours\.router needs FastAPI: install the kit with its fastapi extra, eifi1-server-kit\[fastapi\]",
    ):
        importlib.import_module("eifi1_server_kit.tours.router")


def test_the_clock_is_one_object_for_both_services() -> None:
    """0.10 moved ``Clock`` and ``utc_now`` to :mod:`eifi1_server_kit.clock`; feedback
    re-exports the same objects under 0.9's names (§6.2.1)."""
    from eifi1_server_kit import clock, feedback
    from eifi1_server_kit.feedback import ports

    assert feedback.utc_now is ports.utc_now is clock.utc_now
    assert feedback.Clock is ports.Clock is clock.Clock
    assert "Clock" in ports.__all__ and "utc_now" in ports.__all__
