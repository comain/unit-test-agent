"""Which backend implements what, as data rather than as imports.

`engine` is meant to be the language-agnostic layer and `language/{java,python}`
the backends under it. It did not read that way: nine modules in `engine` each
carried a factory that imported both backends by name --

    if normalized == "java":
        from uta.language.java.parse import JavaParseProvider
        return JavaParseProvider()

-- so `engine` depended on `language` and `language` depended on `engine`, in a
cycle seventy imports deep. Nothing could be moved into a layer while that held,
because moving either package moved the cycle with it.

The imports are lazy, and that is worth keeping: parsing a Java project should
not pay to import the Python tree-sitter grammar. So the coupling becomes a
string resolved on first use. `engine` now contains no import of `language` at
all, the static graph is acyclic, and nothing is imported any earlier than
before.

Adding a language is a table entry here plus its modules, not an edit to nine
factories that each have their own way of spelling the same if/elif.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Tuple


@dataclass(frozen=True)
class BackendConstructionRequest:
    """Open input bag used by a backend-owned provider factory.

    Shared callers provide resources they already have; each backend declares
    and consumes only the names needed to construct its implementation.
    """

    repo_path: Path
    inputs: Mapping[str, Any] = field(default_factory=dict)

    def require(self, name: str) -> Any:
        value = self.inputs.get(name)
        if value is None:
            raise ValueError(f"backend construction requires input {name!r}")
        return value


class UnknownBackendError(LookupError):
    """No backend is registered for this language and role."""

    def __init__(self, language: str, role: str, known: Iterable[str]):
        self.language = language
        self.role = role
        super().__init__(
            f"no {role} backend for language {language!r}; "
            f"registered: {', '.join(sorted(known)) or 'none'}"
        )


#: ``(language, role) -> "module.path:AttributeName"``, and empty on purpose.
#:
#: This module owns the *mechanism* -- register, look up, fail with a useful
#: message -- and none of the knowledge. The built-in table lives in
#: `uta.composition.language_backends`, above everything it names, because a
#: table here gave the lowest layer fourteen edges into `uta.language` and was
#: the single edge holding a five-package cycle together.
_PROVIDERS: Dict[Tuple[str, str], str] = {}


def normalize_language(language: str) -> str:
    return str(language or "").strip().lower()


def register_backend(language: str, role: str, target: str) -> None:
    """Point a (language, role) at ``"module.path:Attribute"``.

    For a backend shipped outside this package, or replaced in a test.
    """
    _PROVIDERS[(normalize_language(language), role)] = target


def languages_for(role: str) -> Tuple[str, ...]:
    """Every language with a backend for this role."""
    return tuple(sorted(lang for lang, kind in _PROVIDERS if kind == role))


def backend_class(language: str, role: str) -> Any:
    """Resolve the class or module registered for a language and role.

    The import happens here, on first use, which is where it happened before.
    """
    normalized = normalize_language(language)
    try:
        target = _PROVIDERS[(normalized, role)]
    except KeyError:
        raise UnknownBackendError(normalized, role, languages_for(role)) from None
    module_path, has_attr, attribute = target.partition(":")
    mod = import_module(module_path)
    return getattr(mod, attribute) if has_attr else mod


def make_backend(language: str, role: str, *args: Any, **kwargs: Any) -> Any:
    """Resolve and construct in one step -- what every factory wanted."""
    return backend_class(language, role)(*args, **kwargs)


def construct_backend(
    language: str,
    role: str,
    request: BackendConstructionRequest,
) -> Any:
    """Construct a provider while leaving asymmetric inputs backend-owned."""
    factory = make_backend(language, role)
    required = tuple(sorted(getattr(factory, "required_inputs", ())))
    missing = [name for name in required if request.inputs.get(name) is None]
    if missing:
        descriptions = getattr(factory, "input_descriptions", {})
        rendered = [
            f"{name} ({descriptions[name]})" if name in descriptions else name
            for name in missing
        ]
        raise ValueError(
            f"{normalize_language(language)} {role} requires backend input(s): "
            f"{', '.join(rendered)}"
        )
    create = getattr(factory, "create", None)
    if not callable(create):
        raise TypeError(f"{normalize_language(language)} {role} backend does not define create(request)")
    return create(request)


def make_all(role: str, *args: Any, **kwargs: Any) -> Tuple[Any, ...]:
    """One instance per registered language, for the registries that hold a set."""
    return tuple(make_backend(lang, role, *args, **kwargs) for lang in languages_for(role))


def backend_factory(role: str) -> Callable[..., Any]:
    """A ``(language, *args) -> instance`` callable for one role."""

    def factory(language: str, *args: Any, **kwargs: Any) -> Any:
        return make_backend(language, role, *args, **kwargs)

    return factory
