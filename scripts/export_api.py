"""Export the kit's public API as JSON for the ui-kit showcase's "Server kit" group.

Marcel's decision (2026-10-07): the server kit's documentation joins the ui-kit showcase as
one site. ``scripts/check.sh`` runs this right after ``uv build``, so ``dist/`` holds
``server-kit-api.json`` beside the wheel, and ``release.yml`` (which uploads ``dist/*``)
attaches it to every release; the showcase pins a release's file, as an app pins the wheel.

Read from the INSTALLED package (``import eifi1_server_kit``) and its source, never by
hand, so it cannot drift from the code. Format version 1::

    {format: "eifi1-server-kit-api", version: 1, kit_version,
     modules: [{name, summary, doc, contract?: {doc, section}, members: [
       {name, kind: "function" | "class" | "model" | "enum" | "constant" | "protocol",
        signature, doc,
        fields?: [{name, type, default, doc}],   # pydantic models, dataclasses, named tuples
        values?: [{name, value}],                # enums
        methods?: [{name, signature, doc}]}]}],
     mails: [{id, title, locale, subject, html}]}

* **members** are each module's ``__all__``, in its order — public names only.
* **signature** is the code as written: ``pick[T](locale: str | None, texts: Mapping[str,
  T], *, fallback: Sequence[str] = FALLBACK_LOCALES) -> T`` for a function (``async``
  first for a coroutine, decorators first for a method), the class statement for a class
  (``@dataclass(frozen=True, slots=True) class Budget``), and the assignment for a constant
  (``RESET_TTL = timedelta(hours=1)``). Names, not values, so no memory address and no
  machine's path ever reaches the file.
* **doc** is the docstring verbatim (dedented), Sphinx roles and all — the showcase renders
  them. A constant's or a field's doc is its ``#:`` comment, as Sphinx reads it.
* **fields**: ``default`` is ``null`` for a required field, else the default's ``repr`` (or
  ``factory()``); a class-variable knob (``offered_locales``) is listed too, typed
  ``ClassVar[…]``.
* **contract** is the ui-kit document and section the module implements, a path in
  ``Eifi1/ui-kit``.
* **mails** are :func:`~eifi1_server_kit.mail.render_mail` over SYNTHETIC sample texts
  ("Ada's Garden Planner", ``example.com``) in ``en`` and ``de-CH``, so the showcase can
  preview the one layout every app's mails share. No real person, product or address.

Run ``uv run python scripts/export_api.py [--out PATH]``; the default is
``dist/server-kit-api.json``.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import enum
import importlib
import inspect
import json
import re
import sys
import textwrap
import typing
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pydantic

import eifi1_server_kit
from eifi1_server_kit.mail import MailText, render_mail

FORMAT = "eifi1-server-kit-api"
VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "dist" / "server-kit-api.json"
PACKAGE = "eifi1_server_kit"

#: Every public module, in the showcase's order, with the ui-kit contract it implements.
MODULES: tuple[tuple[str, str, str], ...] = (
    ("eifi1_server_kit.auth", "docs/auth-harmonization.md", "§8"),
    ("eifi1_server_kit.user_admin", "docs/user-admin-harmonization.md", "§7"),
    ("eifi1_server_kit.settings", "docs/settings-harmonization.md", "§6"),
    ("eifi1_server_kit.demo", "docs/landing-demo-harmonization.md", "§6"),
    ("eifi1_server_kit.billing", "docs/billing-harmonization.md", "§10"),
    ("eifi1_server_kit.mail", "docs/auth-harmonization.md", "§8"),
    ("eifi1_server_kit.feedback", "docs/feedback-harmonization.md", "§3"),
    ("eifi1_server_kit.uploads", "docs/feedback-harmonization.md", "§3.5"),
    ("eifi1_server_kit.limiter", "docs/feedback-harmonization.md", "§3.5, §3.6"),
    ("eifi1_server_kit.errors", "docs/feedback-harmonization.md", "§3.7"),
    ("eifi1_server_kit.translation_review", "docs/i18n-harmonization.md", "§2"),
    # The showcase reviews translations across origins: the review contract's routes.
    ("eifi1_server_kit.cors", "docs/i18n-harmonization.md", "§2"),
)

Json = dict[str, Any]

_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+")


def _repr(value: object) -> str:
    """``repr`` without a memory address, so the file is the same on every run."""
    return _ADDRESS.sub("", repr(value))


# --- source ------------------------------------------------------------------------------


@cache
def _module_source(module_name: str) -> tuple[str, ast.Module]:
    source = inspect.getsource(importlib.import_module(module_name))
    return source, ast.parse(source)


def _object_source(obj: object) -> tuple[list[str], ast.stmt] | None:
    """The dedented source lines of a class or function and its statement; ``None`` for an
    object without source (a generated class)."""
    try:
        source = textwrap.dedent(inspect.getsource(obj))  # type: ignore[arg-type]
    except OSError, TypeError:
        return None
    return source.splitlines(), ast.parse(source).body[0]


def _comment_doc(lines: Sequence[str], lineno: int) -> str:
    """The ``#:`` comment block right above line ``lineno`` (1-based), as Sphinx reads it."""
    block: list[str] = []
    index = lineno - 2
    while index >= 0 and lines[index].strip().startswith("#:"):
        block.insert(0, lines[index].strip()[2:].removeprefix(" "))
        index -= 1
    return "\n".join(block)


def _docstring(node: ast.AST | None) -> str:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module):
        return ast.get_docstring(node) or ""
    return ""


# --- signatures --------------------------------------------------------------------------


def _parameter(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation is not None:
        text += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            text += f" = {ast.unparse(default)}"
    elif default is not None:
        text += f"={ast.unparse(default)}"
    return text


def _parameters(args: ast.arguments, *, drop_first: bool) -> str:
    """PEP 8's spacing (``x: int = 1``, ``x=1``), which ``ast.unparse`` does not keep."""
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults += args.defaults
    pairs = list(zip(positional, defaults, strict=True))
    positional_only = len(args.posonlyargs)
    if drop_first and pairs:
        pairs = pairs[1:]
        positional_only = max(positional_only - 1, 0)
    parts = [_parameter(arg, default) for arg, default in pairs]
    if positional_only:
        parts.insert(positional_only, "/")
    if args.vararg is not None:
        parts.append("*" + _parameter(args.vararg, None))
    elif args.kwonlyargs:
        parts.append("*")
    parts += [_parameter(arg, default) for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)]
    if args.kwarg is not None:
        parts.append("**" + _parameter(args.kwarg, None))
    return ", ".join(parts)


def _type_params(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    return f"[{', '.join(ast.unparse(param) for param in node.type_params)}]" if node.type_params else ""


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    return [ast.unparse(decorator) for decorator in node.decorator_list]


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, *, method: bool = False) -> str:
    decorators = _decorators(node)
    prefix = "".join(f"@{decorator} " for decorator in decorators)
    if isinstance(node, ast.AsyncFunctionDef):
        prefix += "async "
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    if {"property", "computed_field"} & set(decorators):
        return f"{prefix}{node.name}{returns}"
    drop_first = method and "staticmethod" not in decorators
    return f"{prefix}{node.name}{_type_params(node)}({_parameters(node.args, drop_first=drop_first)}){returns}"


def _class_signature(node: ast.ClassDef) -> str:
    prefix = "".join(f"@{decorator} " for decorator in _decorators(node))
    bases = [ast.unparse(base) for base in node.bases]
    bases += [f"{keyword.arg}={ast.unparse(keyword.value)}" for keyword in node.keywords]
    arguments = f"({', '.join(bases)})" if bases else ""
    return f"{prefix}class {node.name}{_type_params(node)}{arguments}"


# --- members -----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _Declared:
    """A name annotated in a class body: its annotation, its value and its ``#:`` doc."""

    annotation: str
    value: str | None
    doc: str

    @property
    def is_class_var(self) -> bool:
        return self.annotation.startswith(("ClassVar", "typing.ClassVar"))


def _declared(cls: type) -> dict[str, _Declared]:
    """Every annotated name in the bodies of ``cls`` and its kit bases, the nearest winning."""
    declared: dict[str, _Declared] = {}
    for klass in reversed(cls.__mro__):
        if not klass.__module__.startswith(PACKAGE):
            continue
        found = _object_source(klass)
        if found is None:
            continue
        lines, node = found
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                declared[statement.target.id] = _Declared(
                    annotation=ast.unparse(statement.annotation),
                    value=ast.unparse(statement.value) if statement.value is not None else None,
                    doc=_comment_doc(lines, statement.lineno),
                )
    return declared


def _type_name(annotation: object) -> str:
    if isinstance(annotation, type) and not typing.get_args(annotation):
        return annotation.__qualname__
    return _repr(annotation).replace("typing.", "")


def _fields(cls: type) -> list[Json] | None:
    declared = _declared(cls)
    fields: list[Json] = []
    if issubclass(cls, pydantic.BaseModel):
        for name, info in cls.model_fields.items():
            source = declared.get(name)
            if info.is_required():
                default = None
            elif info.default_factory is not None:
                default = f"{getattr(info.default_factory, '__name__', 'factory')}()"
            else:
                default = _repr(info.default)
            fields.append(
                {
                    "name": name,
                    "type": source.annotation if source else _type_name(info.annotation),
                    "default": default,
                    "doc": (source.doc if source else "") or info.description or "",
                }
            )
    elif dataclasses.is_dataclass(cls):
        for item in dataclasses.fields(cls):
            source = declared.get(item.name)
            if item.default is not dataclasses.MISSING:
                default = _repr(item.default)
            elif item.default_factory is not dataclasses.MISSING:
                default = f"{getattr(item.default_factory, '__name__', 'factory')}()"
            else:
                default = None
            fields.append(
                {
                    "name": item.name,
                    "type": source.annotation if source else str(item.type),
                    "default": default,
                    "doc": source.doc if source else "",
                }
            )
    elif issubclass(cls, tuple) and hasattr(cls, "_fields"):
        defaults: Mapping[str, object] = getattr(cls, "_field_defaults", {})
        for name in cls._fields:
            source = declared.get(name)
            fields.append(
                {
                    "name": name,
                    "type": source.annotation if source else "",
                    "default": _repr(defaults[name]) if name in defaults else None,
                    "doc": source.doc if source else "",
                }
            )
    else:
        return None
    # The class-variable knobs an app sets in its subclass (``offered_locales``).
    for name, source in declared.items():
        if source.is_class_var and not name.startswith("_"):
            fields.append(
                {"name": name, "type": source.annotation, "default": _repr(getattr(cls, name)), "doc": source.doc}
            )
    return fields


def _methods(node: ast.ClassDef) -> list[Json]:
    methods: list[Json] = []
    for statement in node.body:
        if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if statement.name.startswith("_") and statement.name not in {"__init__", "__call__"}:
            continue
        methods.append(
            {
                "name": statement.name,
                "signature": _function_signature(statement, method=True),
                "doc": _docstring(statement),
            }
        )
    return methods


def _class_kind(cls: type) -> str:
    if issubclass(cls, enum.Enum):
        return "enum"
    if issubclass(cls, pydantic.BaseModel):
        return "model"
    if typing.is_protocol(cls):
        return "protocol"
    return "class"


def _describe_class(name: str, cls: type) -> Json:
    found = _object_source(cls)
    node = found[1] if found is not None and isinstance(found[1], ast.ClassDef) else None
    member: Json = {
        "name": name,
        "kind": _class_kind(cls),
        "signature": _class_signature(node) if node is not None else f"class {cls.__qualname__}",
        "doc": _docstring(node) if node is not None else inspect.cleandoc(cls.__doc__ or ""),
    }
    if (fields := _fields(cls)) is not None:
        member["fields"] = fields
    if issubclass(cls, enum.Enum):
        member["values"] = [
            {"name": item.name, "value": item.value if isinstance(item.value, str | int) else _repr(item.value)}
            for item in cls
        ]
    if node is not None and (methods := _methods(node)):
        member["methods"] = methods
    return member


def _describe_function(name: str, function: object) -> Json:
    found = _object_source(function)
    if found is not None and isinstance(found[1], ast.FunctionDef | ast.AsyncFunctionDef):
        return {
            "name": name,
            "kind": "function",
            "signature": _function_signature(found[1]),
            "doc": _docstring(found[1]),
        }
    return {  # pragma: no cover - every public function of the kit has source
        "name": name,
        "kind": "function",
        "signature": f"{name}{inspect.signature(function)}",  # type: ignore[arg-type]
        "doc": inspect.cleandoc(getattr(function, "__doc__", None) or ""),
    }


def _assignment(module_name: str, name: str) -> tuple[str, ast.stmt] | None:
    """Where ``name`` is assigned at the top of ``module_name``, following ``from … import``."""
    _, tree = _module_source(module_name)
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            return module_name, statement
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        ):
            return module_name, statement
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module and statement.module.startswith(PACKAGE):
            for alias in statement.names:
                if (alias.asname or alias.name) == name:
                    return _assignment(statement.module, alias.name)
    return None


def _describe_constant(module_name: str, name: str, value: object) -> Json:
    found = _assignment(module_name, name)
    if found is None:  # pragma: no cover - every public constant of the kit is assigned in its source
        return {"name": name, "kind": "constant", "signature": f"{name} = {_repr(value)}", "doc": ""}
    home, statement = found
    source, _ = _module_source(home)
    return {
        "name": name,
        "kind": "constant",
        "signature": ast.get_source_segment(source, statement) or f"{name} = {_repr(value)}",
        "doc": _comment_doc(source.splitlines(), statement.lineno),
    }


def describe_member(module: ModuleType, name: str) -> Json:
    """One public name of ``module`` in the format's shape."""
    value = getattr(module, name)
    if inspect.isclass(value):
        return _describe_class(name, value)
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return _describe_function(name, value)
    return _describe_constant(module.__name__, name, value)


def _summary(doc: str) -> str:
    """The docstring's first paragraph, on one line."""
    return " ".join(doc.split("\n\n", 1)[0].split())


def describe_module(module_name: str, contract_doc: str, section: str) -> Json:
    module = importlib.import_module(module_name)
    exported = getattr(module, "__all__", None)
    if exported is None:
        raise RuntimeError(f"{module_name} has no __all__: say which names are public")
    doc = inspect.cleandoc(module.__doc__ or "")
    return {
        "name": module_name,
        "summary": _summary(doc),
        "doc": doc,
        "contract": {"doc": contract_doc, "section": section},
        "members": [describe_member(module, name) for name in exported],
    }


# --- mails -------------------------------------------------------------------------------

#: Synthetic sample mails for the showcase's preview: (id, title, texts, link, values).
#: "Ada's Garden Planner" is no product, and every address is on example.com.
SAMPLE_MAILS: tuple[tuple[str, str, Mapping[str, MailText], str, Mapping[str, str]], ...] = (
    (
        "password-reset",
        "Reset your password",
        {
            "en": MailText(
                subject="Reset your password",
                intro="Hello {name},",
                body=(
                    "Someone asked to reset the password of your Ada's Garden Planner account. "
                    "The link works for {hours} hour, and only once."
                ),
                cta="Reset your password",
                outro="If that wasn't you, ignore this mail: your password stays as it is.",
            ),
            "de-CH": MailText(
                subject="Passwort zurücksetzen",
                intro="Hallo {name}",
                body=(
                    "Jemand möchte das Passwort Ihres Kontos bei Ada's Garden Planner zurücksetzen. "
                    "Der Link gilt {hours} Stunde lang und nur einmal."
                ),
                cta="Passwort zurücksetzen",
                outro="Falls Sie das nicht waren, ignorieren Sie diese Mail: Ihr Passwort bleibt, wie es ist.",
            ),
        },
        "https://garden.example.com/reset-password?token=sample-token",
        {"name": "Ada", "hours": "1"},
    ),
    (
        "email-change",
        "Confirm your new address",
        {
            "en": MailText(
                subject="Confirm your new address",
                intro="Hello {name},",
                body=(
                    "Confirm that {address} is your new address for Ada's Garden Planner. "
                    "Until you do, you sign in with your current one. The link works for {hours} hours."
                ),
                cta="Confirm the address",
                outro="If you didn't ask for this, ignore this mail and nothing changes.",
            ),
            "de-CH": MailText(
                subject="Bestätigen Sie Ihre neue Adresse",
                intro="Hallo {name}",
                body=(
                    "Bestätigen Sie, dass {address} Ihre neue Adresse bei Ada's Garden Planner ist. "
                    "Bis dahin melden Sie sich mit der bisherigen an. Der Link gilt {hours} Stunden."
                ),
                cta="Adresse bestätigen",
                outro="Falls Sie das nicht verlangt haben, ignorieren Sie diese Mail; es ändert sich nichts.",
            ),
        },
        "https://garden.example.com/settings/account?email-token=sample-token",
        {"name": "Ada", "address": "ada.example@example.com", "hours": "48"},
    ),
    (
        "notice",
        "A plain notice",
        {
            "en": MailText(
                subject="Your password was changed",
                intro="Hello {name},",
                body=(
                    "The password of your Ada's Garden Planner account was changed on {date}. "
                    "If that was you, there is nothing to do."
                ),
                cta="Review your sessions",
                outro="If it wasn't you, reset your password now and write to support@example.com.",
            ),
            "de-CH": MailText(
                subject="Ihr Passwort wurde geändert",
                intro="Hallo {name}",
                body=(
                    "Das Passwort Ihres Kontos bei Ada's Garden Planner wurde am {date} geändert. "
                    "Wenn Sie das waren, ist nichts zu tun."
                ),
                cta="Sitzungen ansehen",
                outro="Falls nicht, setzen Sie Ihr Passwort jetzt zurück und schreiben Sie an support@example.com.",
            ),
        },
        "https://garden.example.com/settings/security",
        {"name": "Ada", "date": "7.10.2026"},
    ),
)

#: The locales every sample is rendered in.
MAIL_LOCALES: tuple[str, ...] = ("en", "de-CH")


def sample_mails() -> list[Json]:
    """Each sample rendered through the kit's one layout, in every locale of
    :data:`MAIL_LOCALES`."""
    mails: list[Json] = []
    for mail_id, title, texts, link, values in SAMPLE_MAILS:
        for locale in MAIL_LOCALES:
            message = render_mail(locale, texts, link=link, **values)
            mails.append(
                {"id": mail_id, "title": title, "locale": locale, "subject": message.subject, "html": message.html}
            )
    return mails


# --- the file ----------------------------------------------------------------------------


def build() -> Json:
    """The whole export, as the JSON object written to the file."""
    return {
        "format": FORMAT,
        "version": VERSION,
        "kit_version": eifi1_server_kit.__version__,
        "modules": [describe_module(name, doc, section) for name, doc, section in MODULES],
        "mails": sample_mails(),
    }


def render(data: Json) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"where to write (default: {DEFAULT_OUT})")
    arguments = parser.parse_args(argv)
    out: Path = arguments.out
    text = render(build())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"{out}: {len(text.encode()):,} bytes, {len(MODULES)} modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
