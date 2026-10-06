"""Mask personal data and secrets in text that goes to logs, traces and audit rows (Step T). Pure functions.

What is masked:
- email addresses and secrets (api key, token, password, Bearer): done by core.logging.mask, reused here
- phone numbers: Pakistani mobile (0300-1234567) and international (+92 300 1234567) -> [phone]
- CNIC with dashes (35202-1234567-1) -> [cnic]
- card numbers: 13 to 19 digits that pass the Luhn check -> [card]

Order numbers, amounts and tracking numbers are left alone, because they are needed to debug a run.
Use mask_data on a whole payload (dict, list, text), for example as the LangSmith input and output hider.
The DeepEval metrics import has_pii and mask_pii, so keep the names stable.
"""
import re
from typing import Any

from shoppilot.core.logging import mask as mask_secrets

_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}[\s-]?\d(?:[\s-]?\d){8,11}(?!\d)")
_PHONE_LOCAL = re.compile(r"(?<!\d)03\d{2}[\s-]?\d{7}(?!\d)")
_CNIC = re.compile(r"(?<!\d)\d{5}-\d{7}-\d(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, char in enumerate(reversed(digits)):
        n = int(char)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "[card]" if _luhn_ok(digits) else match.group()


def mask_pii(text: str) -> str:
    """The text with emails, secrets, phone numbers, CNICs and card numbers hidden."""
    text = mask_secrets(text)
    text = _PHONE_INTL.sub("[phone]", text)
    text = _PHONE_LOCAL.sub("[phone]", text)
    text = _CNIC.sub("[cnic]", text)
    return _CARD.sub(_mask_card, text)


def has_pii(text: str) -> bool:
    """True when mask_pii would change the text."""
    return mask_pii(text) != text


def mask_data(value: Any) -> Any:
    """mask_pii on every string inside a dict, list or tuple. Keys and other types stay as they are."""
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {key: mask_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_data(item) for item in value)
    return value
