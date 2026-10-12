"""Engine arguments every new or modified catalog spec must declare.

Catalog specs serve OpenAI-compatible clients that use tool calls and separate
reasoning output. Admission therefore requires the recipe's engine arguments to
enable automatic tool choice and to name a tool-call parser and a reasoning
parser. Only presence is checked: parser names are model-specific, and whether
the serving image provides a named parser is checked elsewhere.

The rule applies to current (schema-2 and schema-3) specs at publication: to
catalog entries added or modified by a change and to contribution
verification. Schema verification at freeze does not apply it, so experimental
drafts without parsers remain valid. Historical schema-1 records cannot be
added or modified and are not checked.
"""
from __future__ import annotations

from typing import Any

from .schema import FLAG_ASSIGNMENT_RE

# Flag -> whether it takes a non-empty value.
REQUIRED_ENGINE_ARGS = {
    '--enable-auto-tool-choice': False,
    '--tool-call-parser': True,
    '--reasoning-parser': True,
}


class CatalogPolicyError(ValueError):
    pass


def _split_assignments(engine_args: list[str]) -> list[str]:
    tokens = []
    for token in engine_args:
        match = FLAG_ASSIGNMENT_RE.match(token)
        tokens.extend(match.groups() if match else (token,))
    return tokens


def missing_required_engine_args(spec: dict[str, Any]) -> list[str]:
    """Return required flags absent from ``recipe.engine_args`` or lacking a value."""
    engine_args = spec.get('recipe', {}).get('engine_args')
    if not isinstance(engine_args, list) or not all(isinstance(token, str) for token in engine_args):
        raise CatalogPolicyError('recipe.engine_args must be a token list')
    tokens = _split_assignments(engine_args)
    missing = []
    for flag, takes_value in REQUIRED_ENGINE_ARGS.items():
        if flag not in tokens:
            missing.append(flag)
            continue
        index = tokens.index(flag) + 1
        value = tokens[index] if index < len(tokens) else ''
        if takes_value:
            if not value.strip() or value.startswith('--'):
                missing.append(flag + ' value')
        elif (value and not value.startswith('--')) or any(token.startswith(flag + '=') for token in engine_args):
            # A switch takes no value; vLLM would reject the stray token.
            missing.append(flag + ' as a switch without a value')
    return missing


def check_required_engine_args(spec: dict[str, Any], *, label: str) -> None:
    """Raise ``CatalogPolicyError`` naming every missing required engine argument."""
    missing = missing_required_engine_args(spec)
    if missing:
        raise CatalogPolicyError(
            f'{label}: catalog specs require engine arguments '
            '--enable-auto-tool-choice, --tool-call-parser NAME and --reasoning-parser NAME; '
            f'missing: {", ".join(missing)}')
