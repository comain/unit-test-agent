"""Immutable language binding registry for enforcement."""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence
from .contracts import EnforcementLanguageBinding


class EnforcementRegistryError(Exception):
    """Base error for enforcement registry operations."""


class UnknownLanguageError(EnforcementRegistryError, KeyError):
    """Raised when an enforcement binding for a requested language is not registered."""

    def __init__(self, language: str, available: Sequence[str]) -> None:
        self.language = language
        self.available = tuple(available)
        msg = f"Unknown enforcement language '{language}'. Available languages: {list(self.available)}"
        super().__init__(msg)


class DuplicateBindingError(EnforcementRegistryError, ValueError):
    """Raised when duplicate language bindings are supplied during registry construction."""

    def __init__(self, language: str) -> None:
        self.language = language
        super().__init__(f"Duplicate enforcement binding for language '{language}'")


class EnforcementRegistry:
    """Immutable registry mapping normalized language names to enforcement bindings."""

    __slots__ = ("_bindings",)

    def __init__(
        self,
        bindings: Iterable[EnforcementLanguageBinding] | Mapping[str, EnforcementLanguageBinding] = (),
    ) -> None:
        table: dict[str, EnforcementLanguageBinding] = {}
        items = bindings.values() if isinstance(bindings, Mapping) else bindings
        for binding in items:
            raw_lang = getattr(binding, "language", None)
            if not raw_lang or not isinstance(raw_lang, str) or not raw_lang.strip():
                raise ValueError("Enforcement binding must declare a non-empty string 'language'")
            norm = raw_lang.strip().lower()
            if norm in table:
                raise DuplicateBindingError(norm)
            table[norm] = binding
        self._bindings: dict[str, EnforcementLanguageBinding] = dict(table)

    def normalize_language(self, language: str) -> str:
        if not language or not isinstance(language, str):
            return ""
        return language.strip().lower()

    def get(self, language: str) -> EnforcementLanguageBinding:
        """Retrieve binding for language or raise UnknownLanguageError."""
        norm = self.normalize_language(language)
        if not norm or norm not in self._bindings:
            raise UnknownLanguageError(language, self.supported_languages())
        return self._bindings[norm]

    def lookup(self, language: str) -> EnforcementLanguageBinding:
        """Alias for get()."""
        return self.get(language)

    def supported_languages(self) -> tuple[str, ...]:
        """Return tuple of all registered normalized language names."""
        return tuple(sorted(self._bindings.keys()))

    @property
    def languages(self) -> tuple[str, ...]:
        """Return tuple of all registered normalized language names."""
        return self.supported_languages()

    def __contains__(self, language: object) -> bool:
        if not isinstance(language, str):
            return False
        return self.normalize_language(language) in self._bindings

    def __getitem__(self, language: str) -> EnforcementLanguageBinding:
        return self.get(language)

    def __len__(self) -> int:
        return len(self._bindings)

    def __repr__(self) -> str:
        return f"EnforcementRegistry(languages={list(self.supported_languages())})"
