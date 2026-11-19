"""Read protocol clauses and project the same records for retrieval."""

from copy import deepcopy
import json
import math
from pathlib import Path
import re


class ProtocolError(ValueError):
    """The book is invalid or does not contain the requested clause."""


_STUDIO_REVISION = "852f0c69ab974540750d61bd1dbfafd56bfd80bb"

# Canonical observation tags and units from this revision's simulation/schema.py.
_STANDARD_UNITS = (
    "kscmh", "kg/h", "kg/h", "kscmh", "kscmh", "kscmh",  # XMEAS(1–6)
    "kPa gauge", "%", "degC", "kscmh",                          # XMEAS(7–10)
    "degC", "%", "kPa gauge", "m3/h",                          # XMEAS(11–14)
    "%", "kPa gauge", "m3/h", "degC", "kg/h", "kW",              # XMEAS(15–20)
    "degC", "degC",                                             # XMEAS(21–22)
) + ("mol %",) * 19                                              # XMEAS(23–41)
_ADDITIONAL_UNITS = (
    ("degC",) * 5 + ("m3/h", "degC", "m3/h") + ("mol %",) * 24    # XMEASADD(1–32)
)
_OBSERVATION_UNITS = {
    **{f"XMEAS({index})": unit for index, unit in enumerate(_STANDARD_UNITS, start=1)},
    **{f"XMEASADD({index})": unit for index, unit in enumerate(_ADDITIONAL_UNITS, start=1)},
}
_UNIT_ALIASES = {"kPa(g)": "kPa gauge", "°C": "degC", "m³/h": "m3/h", "mol%": "mol %"}

# Match a full key before a short ID. Keep Korean suffixes such as ``RC-02로``.
_KEY_TOKEN = r"[A-Za-z0-9_-]+(?:[.+][A-Za-z0-9_-]+)*"
_CLAUSE_REFERENCE = re.compile(
    rf"(?<![A-Za-z0-9_@/-])(?:{_KEY_TOKEN}@{_KEY_TOKEN}/{_KEY_TOKEN}|(?:RC|SEP|STR)-[0-9]+)"
    r"(?![A-Za-z0-9_@/-])"
)


def _object(value, where):
    if not isinstance(value, dict):
        raise ProtocolError(f"{where} must be an object")
    return value


def _items(value, where):
    if not isinstance(value, list) or not value:
        raise ProtocolError(f"{where} must be a non-empty array")
    return value


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{where} must be a non-empty string")
    return value


def _text_items(value, where):
    return [_text(item, f"{where}[{index}]") for index, item in enumerate(_items(value, where))]


def _key_part(value, where):
    value = _text(value, where)
    if re.search(r"[\s@/]", value):
        raise ProtocolError(f"{where} may not contain whitespace, @ or /")
    return value


def _integer(value, minimum, where):
    if type(value) is not int or value < minimum:
        raise ProtocolError(f"{where} must be an integer >= {minimum}")


def _positive_minutes(value, where):
    if (
        type(value) not in (int, float)
        or value <= 0
        or (type(value) is float and not math.isfinite(value))
    ):
        raise ProtocolError(f"{where} must be a finite number greater than zero")


def _choice(value, choices, where):
    if value not in choices:
        raise ProtocolError(f"{where} must be one of {', '.join(choices)}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ProtocolError(f"non-finite JSON number {text!r}")
    return value


def _observation(value, where):
    item = _object(value, where)
    tag = _text(item.get("tag"), f"{where}.tag")
    unit = _text(item.get("unit"), f"{where}.unit")
    expected_unit = _OBSERVATION_UNITS.get(tag)
    if expected_unit is None:
        raise ProtocolError(f"{where}.tag {tag!r} is not an observation in the pinned TEP Studio catalog")
    if _UNIT_ALIASES.get(unit, unit) != expected_unit:
        raise ProtocolError(f"{where}.unit for {tag} must be {expected_unit!r}; got {unit!r}")
    _integer(item.get("min_samples"), 1, f"{where}.min_samples")
    _positive_minutes(item.get("window_min"), f"{where}.window_min")
    return _text(item.get("source_id"), f"{where}.source_id")


def _action(value, where):
    item = _object(value, where)
    identifier = _key_part(item.get("id"), f"{where}.id")
    _choice(item.get("policy"), ("ALLOW", "REQUIRE", "FORBID"), f"{where}.policy")
    for field in ("actor", "when_ko", "instruction_ko"):
        _text(item.get(field), f"{where}.{field}")
    if "approval_role" not in item:
        raise ProtocolError(f"{where}.approval_role is required (use null when appropriate)")
    if item["approval_role"] is not None:
        _text(item["approval_role"], f"{where}.approval_role")
    return identifier


class _Clause:
    """Own a clause's local requirements and its source references."""

    def __init__(self, value, where):
        data = _object(value, where)
        self.id = _key_part(data.get("id"), f"{where}.id")
        _text_items(data.get("equipment"), f"{where}.equipment")
        _integer(data.get("mode"), 1, f"{where}.mode")
        _choice(data.get("control"), ("closed_loop", "open_loop"), f"{where}.control")
        title = _object(data.get("title"), f"{where}.title")
        terms = _object(data.get("terms"), f"{where}.terms")
        for language in ("ko", "en"):
            _text(title.get(language), f"{where}.title.{language}")
            _text_items(terms.get(language), f"{where}.terms.{language}")
        _text(data.get("applies_when_ko"), f"{where}.applies_when_ko")

        source_ids = _text_items(data.get("source_ids"), f"{where}.source_ids")
        observations = _items(data.get("required_observations"), f"{where}.required_observations")
        for index, observation in enumerate(observations):
            source_ids.append(_observation(observation, f"{where}.required_observations[{index}]"))

        action_ids = set()
        for index, action in enumerate(_items(data.get("actions"), f"{where}.actions")):
            action_id = _action(action, f"{where}.actions[{index}]")
            if action_id in action_ids:
                raise ProtocolError(f"{where}.actions has duplicate id {action_id!r}")
            action_ids.add(action_id)

        close = _object(data.get("close"), f"{where}.close")
        _text(close.get("recovery_ko"), f"{where}.close.recovery_ko")
        _positive_minutes(close.get("review_within_min"), f"{where}.close.review_within_min")
        _text(close.get("handoff_role"), f"{where}.close.handoff_role")

        self._data = data
        self.source_ids = tuple(dict.fromkeys(source_ids))

    def clause_references(self):
        data = self._data
        sections = [data["applies_when_ko"], data["close"]["recovery_ko"]]
        for action in data["actions"]:
            sections.extend((action["when_ko"], action["instruction_ko"]))
        return dict.fromkeys(_CLAUSE_REFERENCE.findall("\n".join(sections)))

    def export_fields(self):
        data = self._data
        observations = ", ".join(
            f"{item['tag']} ({item['unit']})" for item in data["required_observations"]
        )
        close = data["close"]
        instructions = [
            f"{action['policy']} {action['id']} · 담당 {action['actor']}"
            f" · 승인 {action['approval_role'] or '별도 역할 없음'}\n"
            f"{action['when_ko']}\n{action['instruction_ko']}"
            for action in data["actions"]
        ]
        return {
            **data,
            "text_ko": "\n".join([
                data["title"]["ko"], ", ".join(data["terms"]["ko"]),
                data["applies_when_ko"], f"필수 계측 {observations}", *instructions,
                close["recovery_ko"],
                f"확인 기한 {close['review_within_min']}분 · 인수 담당 {close['handoff_role']}",
            ]),
            "text_en": "\n".join([
                data["title"]["en"], ", ".join(data["equipment"]),
                ", ".join(data["terms"]["en"]), observations,
            ]),
        }


class ProtocolBook:
    """Validate one version and resolve its clause keys and references."""

    def __init__(self, document):
        data = deepcopy(_object(document, "book"))
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ProtocolError("book.schema_version must be 1")
        sop_id = _key_part(data.get("sop_id"), "book.sop_id")
        version = _key_part(data.get("version"), "book.version")
        revision = _text(data.get("studio_revision"), "book.studio_revision")
        if revision.lower() != _STUDIO_REVISION:
            raise ProtocolError(f"book.studio_revision must match the pinned commit {_STUDIO_REVISION}")

        policy = _object(data.get("policy"), "book.policy")
        _integer(policy.get("max_retrieval_retries"), 0, "book.policy.max_retrieval_retries")
        _positive_minutes(policy.get("info_wait_min"), "book.policy.info_wait_min")
        sources = _object(data.get("sources"), "book.sources")
        for identifier, source in sources.items():
            _text(identifier, "book.sources key")
            where = f"book.sources[{identifier!r}]"
            _object(source, where)
            _text(source.get("url"), f"{where}.url")
            _text(source.get("locator"), f"{where}.locator")

        clauses = {}
        for index, value in enumerate(_items(data.get("clauses"), "book.clauses")):
            clause = _Clause(value, f"book.clauses[{index}]")
            if clause.id in clauses:
                raise ProtocolError(f"duplicate clause id {clause.id!r}")
            for source_id in clause.source_ids:
                if source_id not in sources:
                    raise ProtocolError(f"clause {clause.id!r} refers to unknown source {source_id!r}")
            clauses[clause.id] = clause
        self._book_id = f"{sop_id}@{version}"
        self._metadata = {
            "schema_version": schema_version, "sop_id": sop_id, "version": version,
            "studio_revision": revision, "policy": policy,
        }
        self._clauses = clauses
        self._sources = sources
        for clause in self._clauses.values():
            for reference in clause.clause_references():
                try:
                    self._resolve_clause(reference)
                except ProtocolError as exc:
                    raise ProtocolError(f"clause {clause.id!r} refers to {exc}") from exc

    @classmethod
    def load(cls, path):
        """Load an entire UTF-8 book before the caller projects any records."""
        path = Path(path)
        try:
            document = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object, parse_float=_finite_float, parse_constant=_finite_float,
            )
            return cls(document)
        except (OSError, ValueError, RecursionError) as exc:
            raise ProtocolError(f"{path}: {exc}") from exc

    @property
    def book_id(self):
        return self._book_id

    def __len__(self):
        return len(self._clauses)

    def _record(self, clause):
        return deepcopy({
            **clause.export_fields(), **self._metadata,
            "key": f"{self.book_id}/{clause.id}",
            "sources": {source_id: self._sources[source_id] for source_id in clause.source_ids},
        })

    def _resolve_clause(self, clause_id):
        prefix = f"{self.book_id}/"
        short_id = clause_id[len(prefix):] if clause_id.startswith(prefix) else clause_id
        if short_id not in self._clauses:
            raise ProtocolError(f"unknown clause {clause_id!r} in {self.book_id}")
        return self._clauses[short_id]

    def show(self, clause_id):
        """Return one record by short ID or this book's full versioned key."""
        return self._record(self._resolve_clause(clause_id))

    def records(self):
        """Yield one record per clause in the order in the source book."""
        for clause in self._clauses.values():
            yield self._record(clause)
