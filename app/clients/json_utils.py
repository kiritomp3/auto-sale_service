from __future__ import annotations

import json
import re
from typing import Any


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _strip_fenced_json(text: str) -> str:
    match = _FENCED_JSON_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _json_span(text: str) -> str:
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return text

    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        return text[start:]
    return text[start : end + 1]


def _escape_control_chars_in_strings(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False

    for char in text:
        if in_string:
            if escaped:
                result.append(char)
                escaped = False
                continue
            if char == "\\":
                result.append(char)
                escaped = True
                continue
            if char == '"':
                result.append(char)
                in_string = False
                continue
            if char == "\n":
                result.append("\\n")
                continue
            if char == "\r":
                result.append("\\r")
                continue
            if char == "\t":
                result.append("\\t")
                continue
            if ord(char) < 0x20:
                result.append(f"\\u{ord(char):04x}")
                continue

            result.append(char)
            continue

        result.append(char)
        if char == '"':
            in_string = True

    return "".join(result)


def _decode_json(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as direct_error:
        decoder = json.JSONDecoder()
        for index, char in enumerate(candidate):
            if char not in "{[":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
                return value
            except json.JSONDecodeError:
                continue
        raise direct_error


def parse_llm_json(text: str) -> Any:
    cleaned = _strip_fenced_json(text)
    candidates = [text.strip(), cleaned, _json_span(cleaned)]
    candidates.extend(_escape_control_chars_in_strings(candidate) for candidate in list(candidates))

    last_error: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _decode_json(candidate)
        except json.JSONDecodeError as error:
            last_error = error

    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("Empty JSON response", text, 0)


def parse_llm_json_object(text: str) -> dict[str, Any]:
    value = parse_llm_json(text)
    if not isinstance(value, dict):
        raise ValueError("AI returned JSON, but the top-level value is not an object.")
    return value
