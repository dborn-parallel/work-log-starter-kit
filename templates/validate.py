#!/usr/bin/env python3
"""Check a summary document against `schema.json` and print each violation."""

import datetime as dt
import json
import re
import sys
from pathlib import Path

SCHEMA = json.loads((Path(__file__).resolve().parent / "schema.json").read_text())
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float)}


def violations(value, schema: dict, path: str = "$") -> list[str]:
    """Return the violations of `value` against the subset of JSON Schema that `schema.json` uses."""
    if "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].rsplit("/", 1)[1]]
    if "enum" in schema:
        return [] if value in schema["enum"] else [f"{path}: {value!r} is not one of {schema['enum']}"]
    expected = TYPES[schema["type"]]
    if not isinstance(value, expected) or isinstance(value, bool):
        return [f"{path}: expected {schema['type']}"]
    found = []
    if expected is str:
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            found.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            found.append(f"{path}: does not match {schema['pattern']}")
        if schema.get("format") == "date-time":
            try:
                if dt.datetime.fromisoformat(value).tzinfo is None:
                    found.append(f"{path}: date-time has no offset")
            except ValueError:
                found.append(f"{path}: not an ISO 8601 date-time")
    elif expected is list:
        for index, item in enumerate(value):
            found += violations(item, schema["items"], f"{path}[{index}]")
    elif expected is dict:
        properties = schema.get("properties", {})
        found += [f"{path}: missing {key}" for key in schema.get("required", []) if key not in value]
        for key, item in value.items():
            if key in properties:
                found += violations(item, properties[key], f"{path}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                found += violations(item, schema["additionalProperties"], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                found.append(f"{path}: unknown field {key}")
    return found


def main() -> int:
    found = violations(json.loads(Path(sys.argv[1]).read_text()), SCHEMA)
    print("\n".join(found) or "valid")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
