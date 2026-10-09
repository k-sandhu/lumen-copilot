"""R9: ordinary Python storage must not retain connector run/tenant state."""

import textwrap
from pathlib import Path

import pytest

from tests.conformance.prohibitions import Violation, scan_package

_DATACLASS = "from dataclasses import dataclass, field\n"
_NAMEDTUPLE = "from typing import NamedTuple\n"


def _scan(tmp_path: Path, body: str) -> list[Violation]:
    package = tmp_path / "synthetic"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return scan_package(package)


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param(
            _DATACLASS
            + '@dataclass(frozen=True)\nclass HealthRecord:\n    label: str = "health"\n',
            id="frozen-payload",
        ),
        pytest.param(
            _NAMEDTUPLE + 'class Base(NamedTuple):\n    label: str = "health"\n'
            "class HealthRecord(Base):\n    pass\n",
            id="namedtuple-payload",
        ),
    ],
)
def test_r11_001_writable_record_namespace_is_rejected(declaration: str, tmp_path: Path) -> None:
    body = (
        declaration
        + """
HEALTH_RECORD = HealthRecord()
def health_email(email: str | None) -> str | None:
    payload = HEALTH_RECORD.__dict__
    value = payload.setdefault("email", email)
    return value if isinstance(value, str) else None
"""
    )
    violations = _scan(tmp_path, body)
    assert any("HEALTH_RECORD" in v.detail for v in violations), violations
    assert any("reflection" in v.detail for v in violations), violations


@pytest.mark.parametrize(
    "hook",
    [
        pytest.param(
            "    def __new__(mcls, name, bases, namespace):\n"
            '        namespace["cache"] = {}\n'
            "        return super().__new__(mcls, name, bases, namespace)\n",
            id="namespace-classvar",
        ),
        pytest.param("    def __call__(cls):\n        return {}\n", id="dict-result"),
    ],
)
def test_r11_002_metaclass_construction_is_rejected(hook: str, tmp_path: Path) -> None:
    body = (
        _DATACLASS
        + "from typing import ClassVar\nclass HealthMeta(type):\n"
        + hook
        + """
@dataclass(frozen=True)
class HealthRecord(metaclass=HealthMeta):
    cache: ClassVar[dict[str, str | None]]
HEALTH_RECORD = HealthRecord()
def health_email(email: str | None) -> str | None:
    cache = HEALTH_RECORD.cache
    return cache.setdefault("email", email)
"""
    )
    violations = _scan(tmp_path, body)
    assert any("HEALTH_RECORD" in v.detail for v in violations), violations
    assert any("metaclass" in v.detail for v in violations), violations


@pytest.mark.parametrize(
    "declaration, expression",
    [
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Record:\n    x: int = 1\n",
            "Record()",
            id="dataclass",
        ),
        pytest.param(
            _NAMEDTUPLE + "class Record(NamedTuple):\n    x: int = 1\n", "Record()", id="namedtuple"
        ),
        pytest.param(
            'from enum import Enum\nclass Kind(Enum):\n    FILE = "file"\n',
            'Kind("file")',
            id="enum-call",
        ),
        pytest.param("class Record:\n    pass\n", "Record()", id="ordinary-class"),
        pytest.param("from pathlib import Path\n", 'Path("a")', id="imported-class"),
    ],
)
@pytest.mark.parametrize(
    "binding",
    [
        pytest.param("VALUE = {expression}\n", id="module"),
        pytest.param("class Holder:\n    VALUE = {expression}\n", id="class-attribute"),
        pytest.param("def run(value={expression}):\n    return value\n", id="default"),
    ],
)
def test_r11_retained_constructor_calls_are_forbidden(
    declaration: str, expression: str, binding: str, tmp_path: Path
) -> None:
    violations = _scan(tmp_path, declaration + binding.format(expression=expression))
    assert any(v.rule == "no-mutable-module-state" for v in violations), violations


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("def run(value):\n    return value.__dict__\n", id="dict"),
        pytest.param("def run(value):\n    return value.__setattr__\n", id="setattr-dunder"),
        pytest.param("def run(value):\n    return value.__delattr__\n", id="delattr-dunder"),
        pytest.param("def run(value):\n    return value.__class__\n", id="class-dunder"),
        pytest.param("def run(value):\n    value.__slots__ = ()\n", id="slots-write"),
        pytest.param("def run(value):\n    value.__slots__[0] = 'cache'\n", id="slots-item-write"),
        pytest.param("def run(value):\n    del value.__slots__\n", id="slots-delete"),
        pytest.param(
            "def run(value):\n    object.__setattr__(value, 'cache', {})\n", id="object-setattr"
        ),
        pytest.param("def run(value):\n    return vars(value)\n", id="vars"),
        pytest.param(
            "from builtins import vars as namespace\n"
            "def run(value):\n    return namespace(value)\n",
            id="vars-alias",
        ),
        pytest.param(
            "import builtins as b\ndef run(value):\n    return b.vars(value)\n", id="vars-qualified"
        ),
        pytest.param(
            "def run(value):\n    return getattr(value, '__dict__')\n", id="literal-getattr"
        ),
        pytest.param(
            "from builtins import getattr as read\n"
            "def run(value):\n    return read(value, '__class__')\n",
            id="literal-getattr-alias",
        ),
        pytest.param(
            "class Meta(type):\n    pass\nclass Record(metaclass=Meta):\n    pass\n", id="metaclass"
        ),
        pytest.param(
            "class Record:\n    def __init_subclass__(cls):\n        pass\n", id="init-subclass"
        ),
        pytest.param(
            "class Record:\n    def __set_name__(self, owner, name):\n        pass\n", id="set-name"
        ),
        pytest.param(
            "class Record:\n    def __prepare__(cls, name, bases):\n        return {}\n",
            id="prepare",
        ),
        pytest.param(
            "class Record:\n    def __setattr__(self, name, value):\n        pass\n",
            id="setattr-definition",
        ),
        pytest.param(
            "class Record:\n    def __delattr__(self, name):\n        pass\n",
            id="delattr-definition",
        ),
        pytest.param(
            "def run(value):\n    attach = setattr\n    attach(value, 'cache', {})\n",
            id="setter-reference-alias",
        ),
        pytest.param(
            "import builtins as b\ndef run(value):\n    remove = b.delattr\n"
            "    remove(value, 'cache')\n",
            id="qualified-setter-reference",
        ),
        pytest.param(
            "class Record:\n    def set(self):\n        setattr(self, '__slots__', ())\n",
            id="local-slots-setter",
        ),
    ],
)
def test_r11_reflection_and_class_hooks_fail_closed(body: str, tmp_path: Path) -> None:
    violations = _scan(tmp_path, body)
    assert any(
        "reflection" in v.detail or "class-building" in v.detail for v in violations
    ), violations


@pytest.mark.parametrize("setter", ["setattr", "delattr"])
def test_r11_setters_on_nonlocal_objects_fail_closed(setter: str, tmp_path: Path) -> None:
    args = ", {}" if setter == "setattr" else ""
    violations = _scan(tmp_path, f"def run(value):\n    {setter}(value, 'cache'{args})\n")
    assert any("attributes" in v.detail for v in violations), violations


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param(
            "from enum import Enum\nclass Kind(Enum):\n    FILE = []\n", id="mutable-member"
        ),
        pytest.param(
            "from enum import Enum\nclass Meta(type):\n    pass\n"
            'class Kind(Enum, metaclass=Meta):\n    FILE = "file"\n',
            id="enum-metaclass",
        ),
        pytest.param(
            "from enum import Enum\nclass Base(Enum):\n"
            "    def __init_subclass__(cls):\n        pass\n"
            'class Kind(Base):\n    FILE = "file"\n',
            id="inherited-hook",
        ),
        pytest.param(
            "from enum import Enum\nclass Mixin:\n    pass\n"
            'class Kind(Mixin, Enum):\n    FILE = "file"\n',
            id="unknown-mixin",
        ),
        pytest.param("from external import Kind\n", id="unavailable-enum"),
        pytest.param("from app.domain.entities import WebSourceMode as Kind\n", id="external-enum"),
    ],
)
def test_r11_unproved_enum_member_is_forbidden(declaration: str, tmp_path: Path) -> None:
    member = "PAGE" if "WebSourceMode" in declaration else "FILE"
    violations = _scan(tmp_path, declaration + f"VALUE = Kind.{member}\n")
    assert any("`VALUE`" in v.detail for v in violations), violations


def test_r11_same_package_enum_reference_is_allowed(tmp_path: Path) -> None:
    package = tmp_path / "synthetic"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .kinds import Kind\nVALUE = Kind.FILE\n", encoding="utf-8"
    )
    (package / "kinds.py").write_text(
        'from enum import Enum\nclass Kind(Enum):\n    FILE = "file"\n', encoding="utf-8"
    )
    assert scan_package(package) == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Record:\n    x: int = 1\n"
            "def run():\n    return Record()\n",
            id="fresh-record",
        ),
        pytest.param(
            _DATACLASS + "from dataclasses import asdict\n@dataclass(frozen=True)\n"
            "class Record:\n    x: int = 1\n"
            "def run():\n    payload = asdict(Record())\n"
            "    payload['email'] = 'local'\n    return payload\n",
            id="snapshot-serializer",
        ),
        pytest.param(
            "from enum import Enum\nclass Base(Enum):\n    pass\n"
            'class Kind(str, Base):\n    FILE = "file"\nVALUE = Kind.FILE\n',
            id="enum-inheritance",
        ),
        pytest.param(
            "class Record:\n    __slots__ = ('x',)\n    def __init__(self):\n        self.x = 1\n",
            id="slots-declaration",
        ),
        pytest.param(
            "class Record:\n    def set(self):\n        setattr(self, 'x', 1)\n"
            "    def delete(self):\n        delattr(self, 'x')\n"
            "def run():\n    return Record()\n",
            id="local-instance-setters",
        ),
    ],
)
def test_r11_call_local_helpers_and_snapshots_are_allowed(body: str, tmp_path: Path) -> None:
    assert _scan(tmp_path, body) == []


@pytest.mark.parametrize("name", ["web", "gdrive"])
def test_r11_registered_entrypoints_retain_class_declarations(name: str) -> None:
    import inspect

    from app.connectors.registry import get_connector

    connector = get_connector(name)
    assert inspect.isclass(connector), "CONNECTOR must retain a code declaration, never an instance"
    instance = connector()
    assert instance.name == connector.name == name
    for method in ("validate_config", "sync", "health", "oauth_spec", "fetch_changes", "map_acl"):
        if hasattr(connector, method):
            assert inspect.signature(getattr(connector, method)) == inspect.signature(
                getattr(instance, method)
            )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            'LABEL, CACHE, TTL = ("health", *({}, 60))\n'
            "def remember(email):\n    cache = CACHE\n"
            '    previous = cache.get("email")\n    cache["email"] = email\n'
            "    return previous or email\n",
            id="r10-001-exact-rhs-star",
        ),
        pytest.param("A, (B, C) = (1, (2, *(3,)))\n", id="nested-rhs-star"),
        pytest.param("A, B, C = (1, *(2, 3))\n", id="immutable-rhs-star"),
        pytest.param("VALUE = (1, *(2, 3))\n", id="starred-tuple"),
        pytest.param("A, *B = (1, 2, 3)\n", id="starred-target"),
        pytest.param("[A, B] = (1, 2)\n", id="list-target"),
        pytest.param("A, B = (1,)\n", id="unmatched-targets"),
        pytest.param("A = B = 1\n", id="chained-assignment"),
        pytest.param("VALUE = 1\nVALUE += 2\n", id="scalar-augmented"),
        pytest.param("if (VALUE := 1):\n    pass\n", id="scalar-walrus"),
        pytest.param("for VALUE in (1, 2):\n    pass\n", id="module-for"),
        pytest.param(
            "from dataclasses import dataclass\n@dataclass(frozen=True)\n"
            "class Seeds:\n    def __iter__(self):\n        yield {}\n"
            "CACHES = tuple(Seeds())\n"
            "def remember(email):\n    cache = CACHES[0]\n"
            '    return cache.setdefault("email", email)\n',
            id="r10-002-exact-custom-iterable",
        ),
        pytest.param(
            "from dataclasses import dataclass\n@dataclass(frozen=True)\n"
            "class Seeds:\n    def __iter__(self):\n        yield {}\n"
            "SEEDS = Seeds()\nCACHES = tuple(SEEDS)\n",
            id="custom-iterable-alias",
        ),
        pytest.param(
            "from dataclasses import dataclass\n@dataclass(frozen=True)\n"
            "class Seeds:\n    def __iter__(self):\n        yield {}\n"
            "CACHES = frozenset(Seeds())\n",
            id="custom-frozenset-iterable",
        ),
        pytest.param('ROWS = [{"email": None}]\nVALUE = tuple(ROWS)\n', id="tuple-list-of-dicts"),
        pytest.param("VALUE = tuple((1, 2))\n", id="tuple-literal-conversion"),
        pytest.param("VALUE = frozenset()\n", id="empty-frozenset-call"),
        pytest.param("ITEMS = (1, 2)\nVALUE = frozenset(ITEMS)\n", id="frozenset-name"),
        pytest.param("VALUE = sorted((1, 2))\n", id="sorted"),
        pytest.param("VALUE = [x for x in (1, 2)]\n", id="comprehension"),
        pytest.param("def make():\n    return (1, 2)\nVALUE = make()\n", id="user-function-result"),
        pytest.param(
            'from types import MappingProxyType\nDATA = {"a": 1}\n'
            "VALUE = MappingProxyType(DATA)\n",
            id="proxy-nonliteral",
        ),
        pytest.param(
            "from types import MappingProxyType\n" 'VALUE = MappingProxyType({**{"a": 1}})\n',
            id="proxy-unpacking",
        ),
        pytest.param('import re\nPATTERN = "x"\nVALUE = re.compile(PATTERN)\n', id="regex-name"),
        pytest.param('import re\nVALUE = re.compile("x", re.I)\n', id="regex-attribute-flags"),
        pytest.param(
            "from dataclasses import dataclass\n@dataclass(frozen=True)\n"
            "class State:\n    value: list\nVALUE = State(())\n",
            id="mutable-field-type",
        ),
        pytest.param(
            "from dataclasses import dataclass, field\n@dataclass(frozen=True)\n"
            "class State:\n    value: tuple = field(default_factory=tuple)\n"
            "VALUE = State()\n",
            id="retained-default-factory",
        ),
        pytest.param("VALUE = lambda x=1: x\n", id="retained-lambda"),
        pytest.param("VALUE = 1 if True else 2\n", id="conditional-value"),
        pytest.param(
            "from datetime import datetime\nVALUE = datetime(2020, 1, 1)\n", id="time-constructor"
        ),
        pytest.param(
            "from app.core.logging import get_logger\nVALUE = get_logger(__name__)\n",
            id="logger-handle",
        ),
        pytest.param(
            "from external import UNKNOWN\nVALUE = UNKNOWN\n", id="unknown-imported-payload"
        ),
    ],
)
def test_r10_only_whitelist_grammar_is_retained(body: str, tmp_path: Path) -> None:
    violations = _scan(tmp_path, body)
    assert any(v.rule == "no-mutable-module-state" for v in violations), violations


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('VALUE = ("a", b"b", 1, 1.5, 1j, True, None, ...)\n', id="constants"),
        pytest.param("VALUE = ((1, 2), ())\n", id="tuple-display"),
        pytest.param("VALUE = frozenset({1, 2})\n", id="frozenset-set"),
        pytest.param("VALUE = frozenset([1, 2])\n", id="frozenset-list"),
        pytest.param("VALUE = frozenset((1, 2))\n", id="frozenset-tuple"),
        pytest.param(
            "from types import MappingProxyType\n"
            'VALUE = MappingProxyType({"a": (1, frozenset({2}))})\n',
            id="proxy",
        ),
        pytest.param('import re\nVALUE = (re.compile("x"), re.compile(b"x", 2))\n', id="regex"),
        pytest.param('VALUE = (-1, 60 * 60, "a" + "b", ~1, 1j + 2)\n', id="constant-operators"),
        pytest.param(
            "BASE = (1, 2)\nVALUE = BASE\nA, (B, C) = (1, (2, 3))\n", id="name-and-unpacking"
        ),
        pytest.param(
            "def helper():\n    pass\nclass State:\n    value = 1\n" "VALUE = (helper, State)\n",
            id="code-declarations",
        ),
        pytest.param(
            "import re\nfrom pathlib import Path\nfrom re import compile\n"
            "VALUE = (re, Path, compile, re.compile)\n",
            id="imported-code",
        ),
        pytest.param("import math\nVALUE = math\n", id="native-module"),
        pytest.param(
            'from enum import Enum\nclass Mode(Enum):\n    READ = "read"\n'
            'VALUE = Mode.READ\ndef run():\n    return Mode("read")\n',
            id="enum",
        ),
        pytest.param(
            "from typing import NamedTuple\nclass State(NamedTuple):\n"
            "    value: int = 1\ndef run():\n    return State()\n",
            id="namedtuple",
        ),
        pytest.param(
            "from dataclasses import dataclass\n@dataclass(frozen=True)\n"
            "class Base:\n    value: int = 1\n@dataclass(frozen=True)\n"
            'class State(Base):\n    label: str = "a"\ndef run():\n    return State()\n',
            id="frozen-inherited",
        ),
        pytest.param(
            "from typing import TYPE_CHECKING, TypeAlias\n"
            "if TYPE_CHECKING:\n    VALUE = []\n"
            "Alias: TypeAlias = tuple[int, ...]\n",
            id="typing-declarations",
        ),
        pytest.param("Alias = tuple[int, ...]\n", id="implicit-type-alias"),
        pytest.param("type Alias = tuple[int, ...]\n", id="pep695-type-alias"),
        pytest.param(
            "def run():\n    A, B, C = (1, *({}, 3))\n" "    return tuple([B])\n",
            id="local-allocation",
        ),
    ],
)
def test_r10_every_grammar_alternative_is_allowed(body: str, tmp_path: Path) -> None:
    assert _scan(tmp_path, body) == []


@pytest.mark.parametrize(
    "import_shape", ["from .constants import VALUE", "from . import constants"]
)
def test_r10_same_package_references_check_definitions(tmp_path: Path, import_shape: str) -> None:
    package = tmp_path / "synthetic"
    package.mkdir()
    expression = "constants.VALUE" if import_shape.endswith("constants") else "VALUE"
    (package / "__init__.py").write_text(
        f"{import_shape}\nRETAINED = {expression}\n", encoding="utf-8"
    )
    constants = package / "constants.py"
    constants.write_text("VALUE = (1, None)\n", encoding="utf-8")
    assert scan_package(package) == []
    constants.write_text("VALUE = {}\n", encoding="utf-8")
    assert any(v.module.endswith("synthetic") for v in scan_package(package))


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "VALUE = None\ndef outer():\n    VALUE = {}\n"
            "    def inner(default=VALUE):\n        return default\n"
            "    return inner\n",
            id="local-default-shadow",
        ),
        pytest.param(
            "VALUE = None\nclass State:\n    VALUE = {}\n"
            "    def inner(self, default=VALUE):\n        return default\n",
            id="class-default-shadow",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Base:\n"
            "    value: list = field(default_factory=list)\n"
            "@dataclass(frozen=True)\nclass State(Base):\n"
            "    value: tuple = ()\nVALUE = State()\n",
            id="inherited-overridden-field",
        ),
    ],
)
def test_r10_field_and_default_references_fail_closed(body: str, tmp_path: Path) -> None:
    violations = _scan(tmp_path, body)
    # Check the retention site itself, not an independent invalid declaration.
    expected = "function/lambda default" if "default=VALUE" in body else "`VALUE`"
    assert any(expected in v.detail for v in violations), violations


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Base:\n"
            "    emails: list[str] = field(default_factory=list)\n"
            "@dataclass(frozen=True)\nclass State(Base):\n    pass\nCACHE = State()\n"
            "def health_email(email):\n    emails = CACHE.emails\n"
            "    previous = emails[0] if emails else email\n"
            "    emails.append(email)\n    return previous\n",
            id="r9-001-exact-inherited-list-factory",
        ),
        pytest.param(
            "def health_email(email, *, cache={}):\n"
            '    previous = cache.get("email")\n    cache["email"] = email\n'
            "    return previous or email\n",
            id="r9-002-exact-keyword-default",
        ),
        pytest.param(
            'remember = lambda email, cache={}: cache.setdefault("email", email)\n',
            id="r9-002-exact-lambda-default",
        ),
        pytest.param("CACHE = ([],)\n", id="module-tuple-deep"),
        pytest.param(
            "from types import MappingProxyType as Proxy\n"
            "CACHE = Proxy({'a': 1}) | Proxy({'b': 2})\n",
            id="mappingproxy-union-returns-dict",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n    value: int = 1\n"
            "    def __add__(self, other):\n        return []\nCACHE = State() + State()\n",
            id="overloaded-immutable-binary-result",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n    value: int = 1\n"
            "    def __neg__(self):\n        return []\nCACHE = -State()\n",
            id="overloaded-immutable-unary-result",
        ),
        pytest.param(
            "from enum import Enum\nclass Mode(Enum):\n    READ = 'read'\n"
            "    def remember(self, email):\n        self.cache = email\nCACHE = Mode.READ\n",
            id="enum-singleton-method-state",
        ),
        pytest.param(
            "from enum import Enum\nclass Base(Enum):\n    pass\nclass Mode(Base):\n"
            "    READ = 'read'\n    def remember(self, email):\n        self.cache = email\n",
            id="inherited-enum-singleton-state",
        ),
        pytest.param(
            "from enum import Enum\nclass Mode(Enum):\n    READ = 'read'\n"
            "    @property\n    def payload(self):\n        return []\nCACHE = Mode.READ.payload\n",
            id="enum-unproved-attribute-result",
        ),
        pytest.param("def tuple():\n    return []\nCACHE = tuple()\n", id="shadowed-constructor"),
        pytest.param(
            _DATACLASS + "def tuple():\n    return []\n@dataclass(frozen=True)\n"
            "class State:\n    value: object = field(default_factory=tuple)\nCACHE = State()\n",
            id="shadowed-factory",
        ),
        pytest.param(
            "def helper():\n    pass\ndef run():\n"
            "    from builtins import setattr as attach\n    attach(helper, 'cache', {})\n",
            id="local-setattr-import-alias",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n    value: object\n"
            "CACHE = frozenset((State([]),))\n",
            id="frozenset-deep",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Base:\n"
            "    value: tuple = field(default_factory=lambda: ([],))\n"
            "@dataclass(frozen=True)\nclass Middle(Base):\n    pass\n"
            "@dataclass(frozen=True)\nclass State(Middle):\n    pass\nCACHE = State()\n",
            id="inherited-lambda-factory-deep",
        ),
        pytest.param(
            _NAMEDTUPLE + "class Base(NamedTuple):\n    value: object = []\n"
            "class State(Base):\n    pass\nCACHE = State()\n",
            id="namedtuple-inherited-default",
        ),
        pytest.param(
            _NAMEDTUPLE + "class Base(NamedTuple):\n    value: int = 1\n"
            "class State(Base):\n    def remember(self):\n        self.cache = {}\n"
            "CACHE = State()\n",
            id="namedtuple-singleton-method-state",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n"
            "    value: object = field(default_factory=lambda: State())\nCACHE = State()\n",
            id="recursive-factory-fails-closed",
        ),
        pytest.param(
            _DATACLASS + "from external import Base\n@dataclass(frozen=True)\n"
            "class State(Base):\n    value: int = 1\nCACHE = State()\n",
            id="unknown-base",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n    value: int = 1\n"
            "    def __post_init__(self):\n        object.__setattr__(self, 'cache', [])\n"
            "CACHE = State()\n",
            id="custom-construction-hook",
        ),
        pytest.param(
            _DATACLASS + "def make():\n    return []\n@dataclass(frozen=True)\n"
            "class State:\n    value: object = field(default_factory=make)\nCACHE = State()\n",
            id="mutable-factory-result",
        ),
        pytest.param(
            "from external import UNKNOWN\ndef helper(cache=UNKNOWN):\n    pass\n",
            id="unknown-imported-default",
        ),
        pytest.param("def helper(cache=[]):\n    pass\n", id="positional-default"),
        pytest.param("async def helper(*, cache=set()):\n    pass\n", id="async-default"),
        pytest.param(
            "class Connector:\n    def helper(self, cache={}):\n        pass\n",
            id="method-default",
        ),
        pytest.param(
            "def outer():\n    def inner(cache=[]):\n        pass\n    return inner\n",
            id="nested-function-default",
        ),
        pytest.param(
            "def outer():\n    return lambda *, cache={}: cache\n",
            id="nested-lambda-default",
        ),
        pytest.param(
            "def outer():\n    class Local:\n        def go(self, cache=[]):\n            pass\n",
            id="nested-method-default",
        ),
        pytest.param("class Connector:\n    cache = {}\n", id="class-body-state"),
        pytest.param(
            _DATACLASS + "from typing import ClassVar\n@dataclass\nclass State:\n"
            "    cache: ClassVar[object] = field(default_factory=list)\n",
            id="dataclass-classvar-metadata",
        ),
        pytest.param(
            _DATACLASS + "from typing import ClassVar\n@dataclass\nclass State:\n"
            "    cache: 'ClassVar[object]' = field(default_factory=list)\n",
            id="string-classvar-metadata",
        ),
        pytest.param(
            "def factory():\n    class Local:\n        cache = {}\n    return Local\n",
            id="nested-class-state",
        ),
        pytest.param(
            "def helper():\n    pass\ndef run():\n    helper.cache = {}\n",
            id="function-attribute",
        ),
        pytest.param(
            "def helper():\n    pass\ndef run():\n    setattr(helper, 'cache', {})\n",
            id="function-setattr",
        ),
        pytest.param(
            "def helper():\n    pass\ndef run():\n    f = helper\n    f.cache = {}\n",
            id="function-attribute-alias",
        ),
        pytest.param(
            "def run():\n    def helper():\n        pass\n    helper.cache = {}\n",
            id="local-function-attribute",
        ),
        pytest.param(
            "def helper():\n    pass\ndef run():\n    delattr(helper, 'cache')\n",
            id="function-delattr",
        ),
        pytest.param(
            "class State:\n    value = 1\ndef run():\n    s = State\n    s.cache = {}\n",
            id="class-attribute-alias",
        ),
        pytest.param(
            "class State:\n    value = 1\ndef run():\n    setattr(State, 'cache', {})\n",
            id="class-setattr",
        ),
        pytest.param(
            "from functools import lru_cache\n@lru_cache(maxsize=128)\n"
            "def helper(email):\n    return email\n",
            id="lru-cache",
        ),
        pytest.param(
            "import functools as ft\n@ft.cache\ndef helper(email):\n    return email\n",
            id="cache-alias",
        ),
        pytest.param(
            "from functools import cached_property\nclass State:\n"
            "    @cached_property\n    def email(self):\n        return 'tenant'\n",
            id="cached-property",
        ),
        pytest.param(
            "from cachetools import cached\n@cached(cache={})\n"
            "def helper(email):\n    return email\n",
            id="similar-memoizer",
        ),
        pytest.param(
            "def memoize(f):\n    cache = {}\n    def wrapped(email):\n"
            "        return cache.setdefault(email, f(email))\n    return wrapped\n"
            "@memoize\ndef helper(email):\n    return email\n",
            id="custom-closure-decorator",
        ),
        pytest.param(
            "def factory():\n    cache = {}\n    def remember(email):\n"
            "        return cache.setdefault('email', email)\n    return remember\n"
            "remember = factory()\n",
            id="import-time-closure",
        ),
        pytest.param(
            "from functools import partial\nremember = partial(dict.setdefault, {}, 'email')\n",
            id="retained-partial",
        ),
        pytest.param("VALUES = (value for value in (1, 2))\n", id="retained-generator"),
        pytest.param("VALUES = iter((1, 2))\n", id="retained-iterator"),
        pytest.param(
            "COUNT = 0\ndef run():\n    global COUNT\n    COUNT += 1\n",
            id="global-rebinding",
        ),
        pytest.param(
            "def factory():\n    count = 0\n    def run():\n        nonlocal count\n"
            "        count += 1\n    return run\n",
            id="nonlocal-without-module-name",
        ),
        pytest.param(
            "class State:\n    def __init__(self):\n        self.cache = {}\n"
            "CACHE = State()\ndef run():\n    c = CACHE\n    c.cache['email'] = 'tenant'\n",
            id="singleton-instance-state",
        ),
    ],
)
def test_r9_retained_state_is_rejected(body: str, tmp_path: Path) -> None:
    violations = _scan(tmp_path, body)
    assert any(v.rule == "no-mutable-module-state" for v in violations), violations
    assert all(v.detail for v in violations)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("VALUE = ('a', 1, None, (False, b'b'))\n", id="deep-tuple"),
        pytest.param("VALUE = frozenset(((1, None),))\n", id="deep-frozenset"),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Inner:\n    value: int\n"
            "@dataclass(frozen=True)\nclass State:\n    value: Inner\n"
            "def run():\n    return (State(Inner(1)),)\n",
            id="nested-frozen-instances",
        ),
        pytest.param(
            "from types import MappingProxyType as Proxy\n"
            "VALUE = Proxy({'a': (1, frozenset(('b',)))})\n",
            id="deep-proxy",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Base:\n    value: tuple = (1,)\n"
            "@dataclass(frozen=True)\nclass State(Base):\n    other: str = 'a'\n"
            "def run():\n    return State()\n",
            id="inherited-frozen-default",
        ),
        pytest.param(
            _NAMEDTUPLE + "class Base(NamedTuple):\n    value: tuple = (1,)\n"
            "class State(Base):\n    pass\ndef run():\n    return State()\n",
            id="inherited-namedtuple-default",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n"
            "    value: tuple = field(default_factory=tuple)\ndef run():\n    return State()\n",
            id="empty-tuple-factory",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n"
            "    value: tuple = field(default_factory=lambda: (1, None))\n"
            "def run():\n    return State()\n",
            id="immutable-lambda-factory",
        ),
        pytest.param(
            _DATACLASS + "def make():\n    return (1, None)\n@dataclass(frozen=True)\n"
            "class State:\n    value: tuple = field(default_factory=make)\n"
            "def run():\n    return State()\n",
            id="immutable-function-factory",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass State:\n"
            "    value: tuple = field(default_factory=frozenset)\n"
            "def run():\n    return State()\n",
            id="empty-frozenset-factory",
        ),
        pytest.param(
            "def outer(value=(1, None), *, flag=True):\n"
            "    def inner(cache=None):\n        cache = {} if cache is None else cache\n"
            "        return cache\n    return inner\n",
            id="nested-none-sentinel",
        ),
        pytest.param("def run():\n    return lambda x=(1,): x\n", id="immutable-lambda-default"),
        pytest.param(
            "class State:\n    value = (1, None)\n    @staticmethod\n"
            "    def helper(*, value='a'):\n        return value\n",
            id="immutable-class-and-method-default",
        ),
        pytest.param(
            _DATACLASS + "from typing import ClassVar\n@dataclass\nclass State:\n"
            "    label: ClassVar[tuple] = (1,)\n"
            "    cache: list = field(default_factory=list)\ndef run():\n    return State()\n",
            id="classvar-and-per-run-factory",
        ),
        pytest.param(
            _DATACLASS + "@dataclass(frozen=True)\nclass Base:\n"
            "    emails: list = field(default_factory=list)\n"
            "@dataclass(frozen=True)\nclass State(Base):\n    pass\n"
            "def health_email(email):\n    cache = State()\n"
            "    cache.emails.append(email)\n    return cache.emails[0]\n",
            id="r9-001-per-run-instance-control",
        ),
        pytest.param(
            "def health_email(email, *, cache=None):\n"
            "    cache = {} if cache is None else cache\n"
            "    return cache.setdefault('email', email)\n",
            id="r9-002-none-sentinel-control",
        ),
        pytest.param("def run():\n    cache = {}\n    return cache\n", id="fresh-local"),
        pytest.param(
            "class Parser:\n    def __init__(self):\n        self.parts = []\n"
            "    def feed(self, item):\n        self.parts.append(item)\n"
            "def run(item):\n    parser = Parser()\n    parser.feed(item)\n"
            "    return parser.parts\n",
            id="per-run-parser-instance-attributes",
        ),
    ],
)
def test_r9_immutable_values_and_per_run_state_are_allowed(body: str, tmp_path: Path) -> None:
    assert _scan(tmp_path, body) == []


def test_r9_registration_guide_example_is_scanned(tmp_path: Path) -> None:
    guide = Path(__file__).resolve().parents[2] / "docs/guides/building-a-connector.md"
    section = guide.read_text(encoding="utf-8").split("```python", 1)[1]
    example = section.split("```", 1)[0]
    # Scan the real registration AND class snippets together: the entrypoint
    # retains a class declaration, with no constructor exemption.
    class_example = guide.read_text(encoding="utf-8").split("```python")[2].split("```", 1)[0]
    package = tmp_path / "acme"
    package.mkdir()
    (package / "__init__.py").write_text(example, encoding="utf-8")
    (package / "connector.py").write_text(class_example, encoding="utf-8")
    assert scan_package(package) == []


def test_r12_002_shipping_checklist_registers_a_class(tmp_path: Path) -> None:
    guide = Path(__file__).resolve().parents[2] / "docs/guides/building-a-connector.md"
    checklist = guide.read_text(encoding="utf-8").split("## 7. Register, verify, ship", 1)[1]
    registration = checklist.split("`CONNECTOR = ", 1)[1].split("`", 1)[0]
    body = f"class YourConnector:\n    pass\nCONNECTOR = {registration}\n"
    assert _scan(tmp_path, body) == []
    assert registration == "YourConnector"


def test_r9_audited_entrypoint_cannot_gain_instance_state(tmp_path: Path) -> None:
    violations = _scan(
        tmp_path,
        _DATACLASS + "@dataclass(frozen=True)\n"
        "class Connector:\n    def health(self, email):\n        self.email = email\n"
        "CONNECTOR = Connector()\n",
    )
    assert any("attribute write" in v.detail for v in violations), violations
