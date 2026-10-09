"""The API export for the ui-kit showcase (``scripts/export_api.py``): it runs, and its JSON
has format version 1's shape — every public module, every exported name, synthetic mails."""

from __future__ import annotations

import importlib
import importlib.util
import json
import pkgutil
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

import eifi1_server_kit

ROOT = Path(__file__).resolve().parents[1]


def _exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_api", ROOT / "scripts" / "export_api.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first, as an import would: its dataclass looks its module up while defined.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- format version 1, as the showcase reads it --------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Field(_Strict):
    name: str
    type: str
    default: str | None
    doc: str


class _Value(_Strict):
    name: str
    value: str | int


class _Method(_Strict):
    name: str
    signature: str = Field(min_length=1)
    doc: str


class _Member(_Strict):
    name: str
    kind: Literal["function", "class", "model", "enum", "constant", "protocol"]
    signature: str = Field(min_length=1)
    doc: str
    fields: list[_Field] | None = None
    values: list[_Value] | None = None
    methods: list[_Method] | None = None


class _Contract(_Strict):
    doc: str = Field(pattern=r"^docs/[a-z0-9-]+-harmonization\.md$")
    section: str = Field(pattern=r"^§\d+(\.\d+)?(, §\d+(\.\d+)?)*$")


class _Module(_Strict):
    name: str
    summary: str = Field(min_length=1)
    doc: str = Field(min_length=1)
    contract: _Contract | None = None
    members: list[_Member] = Field(min_length=1)


class _Mail(_Strict):
    id: str
    title: str
    locale: str
    subject: str
    html: str


class _Export(_Strict):
    format: Literal["eifi1-server-kit-api"]
    version: Literal[1]
    kit_version: str
    modules: list[_Module]
    mails: list[_Mail]


@pytest.fixture(scope="module")
def exporter() -> ModuleType:
    return _exporter()


@pytest.fixture(scope="module")
def data(exporter: ModuleType) -> dict[str, Any]:
    built: dict[str, Any] = exporter.build()
    return built


def test_the_export_has_format_version_1s_shape(data: dict[str, Any]) -> None:
    export = _Export.model_validate(data)
    assert export.kit_version == eifi1_server_kit.__version__
    # It survives the trip through JSON unchanged: nothing but JSON types in it.
    assert json.loads(json.dumps(data)) == data


def test_every_public_module_is_exported_with_its_contract(data: dict[str, Any]) -> None:
    public = {
        f"eifi1_server_kit.{info.name}"
        for info in pkgutil.iter_modules(eifi1_server_kit.__path__)
        if not info.name.startswith("_")
    }
    names = [module["name"] for module in data["modules"]]
    assert len(names) == len(set(names)) and set(names) == public, "a new module joins MODULES in the exporter"
    contracts = {module["name"]: module["contract"] for module in data["modules"]}
    assert all(contract is not None for contract in contracts.values())
    assert contracts["eifi1_server_kit.settings"] == {"doc": "docs/settings-harmonization.md", "section": "§6"}
    assert contracts["eifi1_server_kit.demo"] == {"doc": "docs/landing-demo-harmonization.md", "section": "§6"}
    assert contracts["eifi1_server_kit.billing"] == {"doc": "docs/billing-harmonization.md", "section": "§10, §14"}


def test_the_members_are_each_modules_all_in_order(data: dict[str, Any]) -> None:
    for module in data["modules"]:
        exported = importlib.import_module(module["name"]).__all__
        assert [member["name"] for member in module["members"]] == list(exported), module["name"]


def test_members_are_described_by_kind(data: dict[str, Any]) -> None:
    members = {(module["name"], member["name"]): member for module in data["modules"] for member in module["members"]}

    pick = members["eifi1_server_kit.mail", "pick"]
    assert pick["kind"] == "function"
    assert pick["signature"] == (
        "pick[T](locale: str | None, texts: Mapping[str, T], *, fallback: Sequence[str] = FALLBACK_LOCALES) -> T"
    )
    assert pick["doc"].startswith("The row of ``texts`` for ``locale``")

    status = members["eifi1_server_kit.feedback", "FeedbackStatus"]
    assert status["kind"] == "enum" and status["signature"] == "class FeedbackStatus(str, enum.Enum)"
    assert [value["name"] for value in status["values"]][:3] == ["OPEN", "READY", "IN_PROGRESS"]

    token = members["eifi1_server_kit.auth", "TokenResponse"]
    assert token["kind"] == "model" and token["signature"] == "class TokenResponse[UserT = Any](BaseModel)"
    fields = {field["name"]: field for field in token["fields"]}
    assert fields["access_token"]["default"] is None, "required"
    assert fields["expires_at"] == {
        "name": "expires_at",
        "type": "UtcDateTime | None",
        "default": "None",
        "doc": fields["expires_at"]["doc"],
    }
    assert fields["expires_at"]["doc"].startswith("When the access token ends")

    profile = {field["name"]: field for field in members["eifi1_server_kit.auth", "ProfileUpdate"]["fields"]}
    assert profile["offered_locales"]["type"] == "ClassVar[Sequence[str] | None]", "the knob is listed"

    budget = members["eifi1_server_kit.auth", "Budget"]
    assert budget["signature"] == "@dataclass(frozen=True, slots=True) class Budget"
    assert [field["name"] for field in budget["fields"]] == ["max_hits", "window_seconds"]

    gate = members["eifi1_server_kit.demo", "DemoGate"]
    methods = {method["name"]: method["signature"] for method in gate["methods"]}
    assert methods["admit"].startswith("async admit(ip: str, *, reap: DemoReaper,")
    assert methods["settings"] == "@property settings -> DemoSettings"
    assert methods["__init__"].startswith("__init__(settings: DemoSettings, *, reap_limit: int = REAP_PER_START")

    assert members["eifi1_server_kit.mail", "Mailer"]["kind"] == "protocol"
    ttl = members["eifi1_server_kit.auth", "RESET_TTL"]
    assert ttl["kind"] == "constant" and ttl["signature"] == "RESET_TTL = timedelta(hours=1)"
    assert ttl["doc"].startswith("A password-reset link")
    # 0.7's billing: the port, the Paddle client, the notices; generic named tuples keep their parameter.
    assert members["eifi1_server_kit.billing", "BillingProviderClient"]["kind"] == "protocol"
    assert members["eifi1_server_kit.billing", "PaddleError"]["signature"] == "class PaddleError(BillingError)"
    owed = members["eifi1_server_kit.billing", "OwedNotice"]
    assert owed["signature"] == "class OwedNotice[PayerT](NamedTuple)"
    assert [field["name"] for field in owed["fields"]] == ["payer", "kind", "ends_at", "days_left"]
    assert members["eifi1_server_kit.billing", "PortalTarget"]["signature"].startswith("PortalTarget = Literal[")
    # A constant re-exported from another module is found where it is assigned.
    assert members["eifi1_server_kit.user_admin", "LIKE_ESCAPE"]["signature"] == 'LIKE_ESCAPE = "\\\\"'


def test_nothing_machine_specific_reaches_the_file(exporter: ModuleType, data: dict[str, Any]) -> None:
    text = exporter.render(data)
    assert not re.search(r" at 0x[0-9a-fA-F]+", text), "no memory address"
    assert str(ROOT) not in text and "/home/" not in text, "no path"
    assert exporter.render(exporter.build()) == text, "the same on every run"


def test_the_sample_mails_are_synthetic_and_in_both_locales(data: dict[str, Any]) -> None:
    mails = data["mails"]
    assert {(mail["id"], mail["locale"]) for mail in mails} == {
        (mail_id, locale) for mail_id in ("password-reset", "email-change", "notice") for locale in ("en", "de-CH")
    }
    for mail in mails:
        html = mail["html"]
        assert html.startswith(f'<!doctype html><html lang="{mail["locale"]}">'), mail["id"]
        assert f"<title>{mail['subject']}</title>" in html
        assert "{" not in html, "every placeholder filled"
        assert set(re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", html)) <= {"example.com"}, "on example.com only"
        assert set(re.findall(r'href="https://([^/"]+)', html)) == {"garden.example.com"}
    subjects = {mail["subject"] for mail in mails}
    assert {"Reset your password", "Confirm your new address", "Passwort zurücksetzen"} <= subjects


def test_main_writes_the_file(exporter: ModuleType, data: dict[str, Any], tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "dist" / "server-kit-api.json"
    assert exporter.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == data
    assert f"{len(exporter.MODULES)} modules" in capsys.readouterr().out
    assert exporter.DEFAULT_OUT == ROOT / "dist" / "server-kit-api.json"
