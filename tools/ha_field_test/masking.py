"""Masking for everything the harness prints or saves except the private journal.

The reports are meant to be attached to a public pull request or issue, so nothing in
them may identify the user's home. Everything is named by a neutral label: a device by its
model and a number (``H60A6 #2``), a scanner by its kind and a letter (``local adapter``,
``proxy A``), an area by a number (``area 3``), an entity by its role. The labels are
assigned in a stable order (inventory.py), and the reports are built from them.

As defence in depth, every string saved or printed also goes through :meth:`Masker.text`:

* Each registered identifier becomes its label wherever it appears: a device's config entry
  title, device names, Bluetooth address, registry MACs and serial number; a scanner's
  name, adapter and address; an area's name and id. They are matched as the integration's
  diagnostics match them (``diagnostics._identifier_pattern``): case-insensitive, and a hex
  identifier with ``:``, ``-``, ``_`` or no separators between its bytes, in either byte
  order. A name also matches as the slug Home Assistant made an entity id from
  (``light.<name slug>_main_light``), and an area only as a whole word.
* Then any other MAC-shaped token becomes ``<mac>``, with not even its vendor prefix left,
  and any other Govee-name-shaped token loses the MAC digits it ends in
  (``GVH60A6xxxx``).
* Any hex run of 8 or more digits becomes ``<hex>``, as the integration's diagnostics drop
  them: a frame's hex can carry a MAC or serial split across lines.

No credential reaches any text (``ha_api.scrub_token`` runs first).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import re
from typing import Any

from .ha_api import scrub_token

_SEPARATORS = re.compile(r"[:\-_ ]")
_HEX = re.compile(r"[0-9A-Fa-f]+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_MAC_TOKEN_RE = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}([:_-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4,}(?![0-9A-Fa-f])"
)
_NAME_TOKEN_RE = re.compile(
    r"((?:ihoment|Govee|Minger|GBK)_[0-9A-Za-z]+_|(?<![0-9A-Za-z])GV[HR][0-9A-Za-z]*_"
    r"|(?<![0-9A-Za-z])GV[HR]?[0-9A-Za-z]{4})[0-9A-Fa-f]{4}(?![0-9A-Za-z])"
)
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8,}(?![0-9A-Za-z])")
# A shorter name ("Lamp") is matched only as a whole word, and a shorter hex value (a
# serial like "1") not at all, so neither masks unrelated text.
_MIN_IDENTIFIER_LEN = 6
# An area is matched as a whole word, so a shorter name is safe ("Attic", not "Attics").
_MIN_AREA_LEN = 3
# The reports' own words: a name that is one of them can't be told from the report's text,
# and identifies nothing anyway.
_REPORT_WORDS = frozenset(
    {
        "light",
        "lights",
        "lamp",
        "zone",
        "segment",
        "plug",
        "relay",
        "outlet",
        "main",
        "background",
        "left",
        "right",
        "scene",
        "area",
        "proxy",
        "local",
        "adapter",
        "on",
        "off",
    }
)


def slug(text: str) -> str:
    """Return Home Assistant's slugify, close enough for matching entity ids: ``a_b_c``."""
    return _NON_SLUG.sub("_", text.lower()).strip("_")


def _hex_forms(value: str) -> set[str]:
    """Return a hex identifier with each separator between its bytes, in either byte order."""
    compact = _SEPARATORS.sub("", value)
    forms = {value, compact}
    if len(compact) % 2 == 0 and _HEX.fullmatch(compact):
        octets = [compact[i : i + 2] for i in range(0, len(compact), 2)]
        for ordered in (octets, octets[::-1]):
            forms.update(sep.join(ordered) for sep in ("", ":", "-", "_"))
    return forms


@dataclass(frozen=True)
class _Form:
    label: str
    label_slug: str
    is_name: bool
    whole_word: bool = False


@dataclass
class Masker:
    """Replaces each registered identifier with its label, then generic tokens."""

    _forms: dict[str, _Form] = field(default_factory=dict)
    _pattern: re.Pattern[str] | None = None

    def add(
        self, label: str, *, names: Iterable[str | None] = (), hex_ids: Iterable[str | None] = ()
    ) -> None:
        """Register a device or scanner: its names and its hex identifiers (MACs, serial)."""
        label_slug = slug(label)
        for name in names:
            if not name or len(name) < _MIN_AREA_LEN or name.lower() in _REPORT_WORDS:
                continue
            name_slug = slug(name)
            for form in (name, name_slug, name_slug.replace("_", "")):
                if len(form) >= _MIN_IDENTIFIER_LEN:
                    self._forms.setdefault(form.lower(), _Form(label, label_slug, is_name=True))
                elif len(form) >= _MIN_AREA_LEN and form.lower() not in _REPORT_WORDS:
                    short = _Form(label, label_slug, is_name=True, whole_word=True)
                    self._forms.setdefault(form.lower(), short)
        for value in hex_ids:
            if not value or len(_SEPARATORS.sub("", value)) < _MIN_IDENTIFIER_LEN:
                continue
            for form in _hex_forms(value):
                self._forms.setdefault(form.lower(), _Form(label, label_slug, is_name=False))
        self._pattern = None

    def add_area(self, label: str, *names: str | None) -> None:
        """Register an area: its name and id, matched only as whole words."""
        for name in names:
            if not name or len(name) < _MIN_AREA_LEN or name.lower() in _REPORT_WORDS:
                continue
            for form in (name, slug(name)):
                self._forms.setdefault(
                    form.lower(), _Form(label, slug(label), is_name=True, whole_word=True)
                )
        self._pattern = None

    def _replace(self, match: re.Match[str]) -> str:
        """Return the label for a match; its slug where a name is part of an entity id."""
        form = self._forms[match.group(0).lower()]
        text, start, end = match.string, match.start(), match.end()
        in_id = (start > 0 and text[start - 1] in "._") or text[end : end + 1] == "_"
        return form.label_slug if form.is_name and in_id else form.label

    def _compiled(self) -> re.Pattern[str] | None:
        if self._pattern is None and self._forms:
            # Longest first, so a full form wins over a substring form.
            alternatives = [
                rf"(?<![0-9A-Za-z]){re.escape(f)}(?![0-9A-Za-z])"
                if self._forms[f].whole_word
                else re.escape(f)
                for f in sorted(self._forms, key=len, reverse=True)
            ]
            self._pattern = re.compile("|".join(alternatives), re.IGNORECASE)
        return self._pattern

    def text(self, text: str) -> str:
        """Return ``text`` masked; credentials go first, so no other rule leaves a part behind."""
        text = scrub_token(text)
        if (pattern := self._compiled()) is not None:
            text = pattern.sub(self._replace, text)
        text = _MAC_TOKEN_RE.sub("<mac>", text)
        text = _NAME_TOKEN_RE.sub(r"\1xxxx", text)
        return _HEX_RUN.sub("<hex>", text)

    def data(self, value: Any) -> Any:
        """Return ``value`` (JSON-like) with every string, keys included, masked."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.data(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(v) for v in value]
        return value
