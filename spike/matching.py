"""Template selection by configuration_key (spike)."""
from __future__ import annotations

from dataclasses import dataclass

ConfigurationKey = frozenset  # frozenset[tuple[therapy_id, therapist_id]]


def configuration_key(enrollments: list[tuple[str, str]]) -> frozenset:
    return frozenset(enrollments)


@dataclass
class TemplateEntry:
    template_id: str
    name: str
    key: frozenset
    active: bool = True


class NoMatchingTemplate(Exception):
    pass


class AmbiguousTemplates(Exception):
    pass


def describe(key: frozenset) -> str:
    return " + ".join(f"{therapy} ({therapist})" for therapy, therapist in sorted(key))


def find_template(key: frozenset, templates: list[TemplateEntry]) -> TemplateEntry:
    hits = [t for t in templates if t.active and t.key == key]
    if not hits:
        raise NoMatchingTemplate(
            f"No active template for: {describe(key)}. Ask an administrator to upload one."
        )
    if len(hits) > 1:
        names = ", ".join(t.name for t in hits)
        raise AmbiguousTemplates(
            f"Multiple active templates match {describe(key)}: {names}. An administrator must deactivate all but one."
        )
    return hits[0]
