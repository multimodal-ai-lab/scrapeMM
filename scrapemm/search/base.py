"""What every search provider's query and response have in common: very little, on
purpose.

Search APIs disagree on almost everything -- parameter names, result fields, what a
response contains at all -- so scrapeMM does not squeeze them into one schema. Each
provider gets its own query and response classes that mirror its API (see
`scrapemm.search.serper`). What they share is only the plumbing: how they travel
between client and server, which provider answers a query, and `urls`, which hands the
results straight to `scrapemm.retrieve()`.

Like `scrapemm.common.wire`, this is plain dataclasses with no validation library, as
it ships with the client.
"""

from __future__ import annotations

import datetime as dt
import types
from dataclasses import MISSING, Field, dataclass, field, fields, is_dataclass
from typing import (Any, ClassVar, Generic, Literal, TypeVar, Union, get_args, get_origin,
                    get_type_hints)

WireCase = Literal["snake", "camel"]

_hints_cache: dict[type, dict[str, Any]] = {}


def _camel(name: str) -> str:
    """`image_url` -> `imageUrl`"""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


@dataclass
class WireObject:
    """A dataclass that converts itself to and from the dict it travels as.

    Nested wire objects, lists and optionals are converted along the way, driven by
    the type hints, and dates travel as ISO strings ("2024-05-01"). Values are otherwise
    taken as they come: nothing is coerced, so what a provider sent is what the caller
    gets.

    The key of a field on the wire is its name, spelled as `WIRE_CASE` says, unless
    the field overrides it with `field(metadata={"wire": "someKey"})`. Keys the class
    does not know end up in its `extra` field, if it has one, and are ignored otherwise.
    """

    WIRE_CASE: ClassVar[WireCase] = "snake"

    @classmethod
    def _wire_key(cls, f: Field) -> str:
        if "wire" in f.metadata:
            return f.metadata["wire"]
        return _camel(f.name) if cls.WIRE_CASE == "camel" else f.name

    def to_dict(self) -> dict[str, Any]:
        """The wire form. Leaves out what carries no information (None, empty lists
        and dicts), so that a provider's answer comes out the way it came in."""
        data: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "extra":
                continue
            value = _encode(getattr(self, f.name))
            if value is None or value == [] or value == {}:
                continue
            data[self._wire_key(f)] = value
        data.update(_encode(getattr(self, "extra", None) or {}))
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        if not isinstance(data, dict):
            raise ValueError(f"{cls.__name__} expects a JSON object, got "
                             f"{type(data).__name__}.")
        hints = _type_hints(cls)
        rest = dict(data)
        values: dict[str, Any] = {}
        has_extra = False
        for f in fields(cls):
            if f.name == "extra":
                has_extra = True
                continue
            key = cls._wire_key(f)
            if key in rest:
                values[f.name] = _decode(hints[f.name], rest.pop(key))
        if has_extra:
            values["extra"] = rest
        missing = [f.name for f in fields(cls) if f.name not in values
                   and f.default is MISSING and f.default_factory is MISSING]
        if missing:
            raise ValueError(f"{cls.__name__} is missing {', '.join(missing)}.")
        return cls(**values)


@dataclass
class WireRecord(WireObject):
    """A wire object that keeps whatever it does not know, in `extra`. Everything a
    provider *returns* is one of these: providers add fields over time, and dropping
    them would mean callers could never see them without an update of scrapeMM."""

    extra: dict[str, Any] = field(default_factory=dict, kw_only=True)


@dataclass
class SearchResponse(WireRecord):
    """The answer of one search provider, in that provider's own shape."""

    provider: ClassVar[str]

    @property
    def urls(self) -> list[str]:
        """The pages the results point to, best first, ready for `scrapemm.retrieve()`."""
        raise NotImplementedError


R = TypeVar("R", bound=SearchResponse)


@dataclass
class SearchQuery(WireObject, Generic[R]):
    """A query to one search provider, with that provider's own parameters.

    A query travels under its field names (snake case), because the server validates
    it against this very dataclass. Translating it into the provider's own request is
    `request_body()`'s job."""

    # Set by each subclass, as plain class attributes
    provider: ClassVar[str]
    response_class: ClassVar[type[SearchResponse]]

    def validate(self) -> None:
        """Raises ValueError if the query cannot be sent as it is. Called on both
        sides: by the client before sending, and by the server before searching."""

    def request_body(self) -> dict[str, Any]:
        """The body of the request to the provider's API."""
        return self.to_dict()


def _type_hints(cls: type) -> dict[str, Any]:
    if cls not in _hints_cache:
        _hints_cache[cls] = get_type_hints(cls)
    return _hints_cache[cls]


def _decode(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        options = [arg for arg in get_args(tp) if arg is not type(None)]
        return _decode(options[0], value) if len(options) == 1 else value
    if origin is list:
        if not isinstance(value, list):
            return value
        (item_type,) = get_args(tp) or (Any,)
        return [_decode(item_type, item) for item in value]
    if isinstance(tp, type) and is_dataclass(tp) and issubclass(tp, WireObject):
        return tp.from_dict(value) if isinstance(value, dict) else value
    if tp is dt.date and isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return value  # Left for validate() to refuse
    return value


def _encode(value: Any) -> Any:
    if isinstance(value, WireObject):
        return value.to_dict()
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, dt.date):
        return value.isoformat()
    return value
