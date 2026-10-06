"""Checks on tool arguments and on model-written text (Step T). Pure functions, no network, no database, no model.

Rule: a check that finds a problem raises GuardrailViolation (the tool layer turns it into {"ok": False, ...}),
except reply_text_problem, which returns a problem code because the reply node falls back to a fixed text instead of failing.

- check_order_ref: the order number the model passes must look like an order number, not like a sentence.
- check_recipient: an email may only go to the customer of the ticket or to a staff address on the allow-list.
- check_plain_fields: template fields (written by the model) hold no email address and no link.
- reply_text_problem: the free text of a customer email holds no address, no link and no sign that money has moved.

The DeepEval metrics import these functions, so keep the names stable.
"""
import re
import unicodedata
from collections.abc import Iterable, Mapping

from shoppilot.core.errors import GuardrailViolation

MAX_FIELD_CHARS = 600
MAX_REPLY_CHARS = 500

_ORDER_REF = re.compile(r"[A-Za-z0-9#_-]{1,30}")
_ADDRESS = re.compile(r"""[^@\s<>,;:"'()\[\]\\]+@[^@\s<>,;:"'()\[\]\\]+\.[^@\s<>,;:"'()\[\]\\]+""")
# An email address, a link, or a bare domain: a way to send the reader (or data) somewhere else.
_LINK_OR_ADDRESS = re.compile(r"@|https?:|www\.|://|\b[\w-]+\.(?:com|net|org|pk|io|co|info|xyz|me|app|ly)\b", re.IGNORECASE)
# Words that say money has moved. The email template for a refund is picked by code, so model text may never say it.
_MONEY_DONE = re.compile(
    r"\b(refunded|credited|reimbursed|issued|processed|approved)\b"
    r"|\brefund\s+(has|have|was)\b"
    r"|\brefund\s+(ho\s+gaya|ho\s+chuka|kar\s+di)",
    re.IGNORECASE,
)


def _normal(text: object) -> str:
    return unicodedata.normalize("NFKC", str(text)).strip()


def contains_link_or_address(text: object) -> bool:
    return bool(_LINK_OR_ADDRESS.search(_normal(text)))


def check_order_ref(order_ref: object) -> str:
    """The order number as the shop expects it: letters, digits, # _ - and nothing else (at most 30 characters)."""
    ref = _normal(order_ref)
    if not _ORDER_REF.fullmatch(ref):
        raise GuardrailViolation("the order reference has an invalid format")
    return ref


def check_recipient(recipient: object, *, customer_email: str, staff_emails: Iterable[str] = ()) -> str:
    """The address to send to, but only if it is the ticket's customer or a staff address on the allow-list."""
    address = _normal(recipient).lower()
    allowed = {_normal(customer_email).lower(), *(_normal(e).lower() for e in staff_emails)}
    if not _ADDRESS.fullmatch(address) or address not in allowed:
        raise GuardrailViolation("the recipient is not on the allow-list")
    return address


def check_plain_fields(fields: Mapping[str, object], *, max_chars: int = MAX_FIELD_CHARS) -> None:
    """Template fields written by the model: short, and no email address or link in them."""
    for name, value in fields.items():
        text = _normal(value)
        if len(text) > max_chars:
            raise GuardrailViolation(f"field {name} is longer than {max_chars} characters")
        if _LINK_OR_ADDRESS.search(text):
            raise GuardrailViolation(f"field {name} may not contain an email address or a link")


def reply_text_problem(text: object, *, max_chars: int = MAX_REPLY_CHARS) -> str | None:
    """Why the model-written part of a customer email may not be sent, or None when it is fine.

    Codes: EMPTY, TOO_LONG, LINK_OR_ADDRESS, MONEY_CLAIM.
    """
    clean = _normal(text)
    if not clean:
        return "EMPTY"
    if len(clean) > max_chars:
        return "TOO_LONG"
    if _LINK_OR_ADDRESS.search(clean):
        return "LINK_OR_ADDRESS"
    if _MONEY_DONE.search(clean):
        return "MONEY_CLAIM"
    return None
