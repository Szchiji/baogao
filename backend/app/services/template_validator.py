from __future__ import annotations

from typing import Any

import jsonschema

TEMPLATE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ReportTemplate",
    "type": "object",
    "required": ["schema_version", "template_key", "template_name", "enabled", "fields"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "template_key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$"},
        "template_name": {"type": "string", "minLength": 1, "maxLength": 100},
        "description": {"type": "string", "maxLength": 500, "default": ""},
        "enabled": {"type": "boolean", "default": True},
        "submit_label": {"type": "string", "default": "提交", "maxLength": 20},
        "resubmit_label": {"type": "string", "default": "重新提交", "maxLength": 20},
        "supplement_label": {"type": "string", "default": "补充信息", "maxLength": 20},
        "fields": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {"$ref": "#/$defs/field"},
        },
    },
    "$defs": {
        "field": {
            "type": "object",
            "required": ["key", "label", "field_type"],
            "additionalProperties": False,
            "properties": {
                "key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,63}$"},
                "label": {"type": "string", "minLength": 1, "maxLength": 60},
                "field_type": {
                    "type": "string",
                    "enum": ["text", "textarea", "select", "tags", "media"],
                },
                "required": {"type": "boolean", "default": False},
                "help_text": {"type": "string", "maxLength": 200, "default": ""},
                "min_length": {"type": "integer", "minimum": 0, "maximum": 10000},
                "max_length": {"type": "integer", "minimum": 1, "maximum": 10000},
                "options": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "required": ["value", "label"],
                        "additionalProperties": False,
                        "properties": {
                            "value": {"type": "string", "minLength": 1, "maxLength": 60},
                            "label": {"type": "string", "minLength": 1, "maxLength": 60},
                        },
                    },
                },
                "max_items": {"type": "integer", "minimum": 1, "maximum": 50},
                "normalize": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "lowercase": {"type": "boolean", "default": False},
                        "dedupe": {"type": "boolean", "default": True},
                        "trim": {"type": "boolean", "default": True},
                    },
                },
                "allowed_media": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["photo", "document"]},
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"field_type": {"const": "select"}}},
                    "then": {"required": ["options"]},
                }
            ],
        }
    },
}


def validate_template(data: dict[str, Any]) -> list[dict[str, str]]:
    """Validate template JSON against the schema.

    Returns a list of {json_path, reason} dicts for each error found.
    Empty list means valid.
    """
    errors: list[dict[str, str]] = []

    validator = jsonschema.Draft202012Validator(TEMPLATE_SCHEMA)
    for err in sorted(validator.iter_errors(data), key=lambda e: str(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "$"
        errors.append({"json_path": path, "reason": err.message})

    if not errors:
        # Check for duplicate field keys
        fields = data.get("fields", [])
        seen: set[str] = set()
        for i, field in enumerate(fields):
            key = field.get("key", "")
            if key in seen:
                errors.append(
                    {
                        "json_path": f"fields[{i}].key",
                        "reason": f"Duplicate field key: '{key}'",
                    }
                )
            seen.add(key)

    return errors
