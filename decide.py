"""The ``decide`` tool: raw System One questions in, raw answers out.

The caller writes ``questions`` on every call. This module validates them
against ``schemas/decide.validate.schema.json`` (the strict gate), posts the
request to ``POST /v1/systemone``, and returns ``answers`` unchanged.

The model-facing parameters schema is ``schemas/decide.tool.schema.json``: it
shows the per-type shape (oneOf) without the fine-grained limits. The strict
schema is the one that decides whether a call goes out.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:  # annotations only; imported lazily where it is used
    import jsonschema

from .tool_config import load_tool_config, resolve_model

_SCHEMA_DIR = Path(__file__).with_name("schemas")
_TOOL_SCHEMA_PATH = _SCHEMA_DIR / "decide.tool.schema.json"
_STRICT_SCHEMA_PATH = _SCHEMA_DIR / "decide.validate.schema.json"

TOOL_PARAMETERS: dict[str, Any] = json.loads(_TOOL_SCHEMA_PATH.read_text(encoding="utf-8"))
STRICT_SCHEMA: dict[str, Any] = json.loads(_STRICT_SCHEMA_PATH.read_text(encoding="utf-8"))

DECIDE_SCHEMA: dict[str, Any] = {
    "name": "decide",
    "description": (
        "Ask typed questions about state and get the raw System One answers back. "
        "It returns probabilities and distributions, not a verdict."
    ),
    "parameters": TOOL_PARAMETERS,
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_STATE_CHARS = 200_000
_MAX_QUESTIONS = 32
_MAX_QUESTIONS_BYTES = 200_000

_EXAMPLES = {
    "noul": '{"type": "noul", "instructions": "Is this message about billing?"}',
    "choice": (
        '{"type": "choice", "instructions": "What is the tone?", '
        '"criteria": {"angry": "Upset or hostile", "calm": "Neutral or polite"}}'
    ),
    "score": (
        '{"type": "score", "instructions": "How urgent is this?", '
        '"criteria": ["Can wait", "Needs attention today"]}'
    ),
}


class DecideError(RuntimeError):
    """A caller mistake or an API failure, with a message the model can act on."""


def _path(parts: Any) -> str:
    return ".".join(str(p) for p in parts)


def _describe_expected(error: jsonschema.ValidationError) -> str:
    keyword = error.validator
    value = error.validator_value
    got = error.instance
    got_type = type(got).__name__
    if keyword == "required":
        required = list(value) if isinstance(value, (list, tuple, set)) else []
        missing = [m for m in required if isinstance(got, dict) and m not in got]
        return f"missing required key(s): {missing}"
    if keyword == "minProperties":
        return f"needs at least {value} entries"
    if keyword == "maxProperties":
        return f"allows at most {value} entries"
    if keyword == "minItems":
        return f"needs at least {value} items"
    if keyword == "maxItems":
        return f"allows at most {value} items"
    if keyword == "uniqueItems":
        return "has duplicate items"
    if keyword == "pattern":
        return f"does not match the required form ({value})"
    if keyword == "minLength":
        return "must not be empty"
    if keyword == "maxLength":
        return f"must be at most {value} characters"
    if keyword == "additionalProperties":
        return f"has an unknown key ({error.message})"
    if keyword == "const":
        return f"must be {value!r}"
    if keyword == "type":
        return f"wrong type (expected {value}, got {got_type})"
    if keyword == "anyOf":
        return f"does not match any accepted shape (got {got_type})"
    return f"violates {keyword}"


def _raise_question_error(name: str, question: Any, error: jsonschema.ValidationError) -> None:
    where = f"questions.{name}"
    if getattr(error, "absolute_path", None):
        where += "." + _path(error.absolute_path)
    qtype = question.get("type") if isinstance(question, dict) else None
    example = _EXAMPLES.get(str(qtype), _EXAMPLES["noul"])
    received = repr(error.instance)
    if len(received) > 160:
        received = received[:157] + "..."
    raise DecideError(
        f"{where}: {_describe_expected(error)} (got: {received}). Example: {example}"
    )


def validate_state(state: Any) -> None:
    """Reject anything that is not a non-empty string, object or array."""
    if isinstance(state, str):
        if not state.strip():
            raise DecideError("state: empty. Put the content to judge in it.")
        if len(state) > _MAX_STATE_CHARS:
            raise DecideError(
                f"state: must be at most {_MAX_STATE_CHARS} characters (got {len(state)})."
            )
        return
    if isinstance(state, (dict, list)):
        if not state:
            raise DecideError(f"state: empty {type(state).__name__}. Put content in it.")
        size = len(json.dumps(state, ensure_ascii=False))
        if size > _MAX_STATE_CHARS:
            raise DecideError(
                f"state: serialized size must be at most {_MAX_STATE_CHARS} characters (got {size})."
            )
        return
    raise DecideError(
        f"state: must be a string, object, or array (got {type(state).__name__})."
    )


def _branch_validator(qtype: str) -> "jsonschema.Draft202012Validator":
    # Imported lazily so a missing jsonschema does not break plugin load.
    try:
        import jsonschema
    except ImportError as exc:
        raise DecideError(
            "decide: jsonschema is required. Install it with `pip install jsonschema` "
            "(or `hermes plugins update`)."
        ) from exc

    branch = STRICT_SCHEMA["$defs"][qtype]
    subschema = {"$schema": STRICT_SCHEMA.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
                 "$defs": STRICT_SCHEMA["$defs"], **branch}
    return jsonschema.Draft202012Validator(subschema)


def validate_questions(questions: Any) -> None:
    """Validate the named questions against the strict per-type schemas."""
    if not isinstance(questions, dict):
        raise DecideError(
            f"questions: an object is required (got {type(questions).__name__})."
        )
    if not questions:
        raise DecideError("questions: at least one question is required.")
    if len(questions) > _MAX_QUESTIONS:
        raise DecideError(
            f"questions: at most {_MAX_QUESTIONS} questions (got {len(questions)})."
        )
    size = len(json.dumps(questions, ensure_ascii=False))
    if size > _MAX_QUESTIONS_BYTES:
        raise DecideError(
            f"questions: serialized size must be at most {_MAX_QUESTIONS_BYTES} characters."
        )

    for name, question in questions.items():
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise DecideError(
                f"questions.{name}: question names must be lowercase snake_case "
                "(start with a letter, letters/digits/_, up to 64 chars). "
                "Example: tone, wrong_recipient"
            )
        if not isinstance(question, dict):
            raise DecideError(
                f"questions.{name}: an object is required (got {type(question).__name__}). "
                f"Example: {_EXAMPLES['noul']}"
            )
        qtype = question.get("type")
        if qtype not in ("noul", "choice", "score"):
            raise DecideError(
                f"questions.{name}.type: must be one of noul / choice / score "
                f"(got {qtype!r}). Example: {_EXAMPLES['noul']}"
            )
        errors = sorted(_branch_validator(qtype).iter_errors(question),
                        key=lambda e: list(e.absolute_path))
        if errors:
            _raise_question_error(name, question, errors[0])


def _systemone_url(api: str) -> str:
    base = api.strip().rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/systemone"


def decide(args: dict, **kwargs: Any) -> str:
    """Tool handler: validate, POST, return ``answers`` as JSON."""
    state = args.get("state")
    validate_state(state)

    questions = args.get("questions")
    validate_questions(questions)

    cfg = load_tool_config()
    _, model, endpoint = resolve_model(cfg, args.get("model"))

    key = os.environ.get(endpoint["key_env"], "").strip()
    if not key:
        raise DecideError(
            f"{endpoint['key_env']} is not set in the environment. "
            f"Configure the credential for endpoint {endpoint['api']}."
        )

    body = {"model": model["base_model"], "state": state, "questions": questions}
    url = _systemone_url(endpoint["api"])
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                url,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise DecideError(
            f"Could not reach the System One API: {type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code != 200:
        raise DecideError(
            f"System One API error: HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise DecideError(
            f"System One API error: HTTP {response.status_code} returned a non-JSON body: "
            f"{response.text[:200]}"
        ) from exc

    answers = data.get("answers") if isinstance(data, dict) else None
    return json.dumps(answers or {}, ensure_ascii=False)
