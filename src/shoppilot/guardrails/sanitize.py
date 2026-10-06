"""Clean text that comes from outside before a model reads it (Step T).

Customer emails, order notes, reviews and product descriptions are untrusted DATA, never instructions.

Two jobs:
1. clean_text / wrap_untrusted: remove what is used to hide or smuggle instructions (invisible characters, markup,
   links, encoded blobs, our own delimiter tags) and cap the length, so the text can sit between data tags.
2. injection_flags: name what looks like an attack. It only FLAGS (for the audit log and the red-team tests).
   It never blocks. The real defence is typed output, the policy engine, validators and human approval.

Pure functions: no network, no database, no model. The DeepEval metrics import these functions, so keep the names stable.
"""
import re
import unicodedata

MAX_CHARS = 4000
# Every tag we put around data. A new tag must be added here, so customer text can never close it early.
DELIMITER_TAGS = ("customer_message", "facts", "outcome")

_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})  # control, format (zero width, bidi, tag characters), unused
_KEEP = frozenset({"\n", "\t"})
# Invisible characters that are only used to hide text. A zero width joiner alone (normal in Urdu) is not flagged.
_HIDING = re.compile("[\u200b\u2060\ufeff\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")

_NAMES = "|".join(DELIMITER_TAGS)
_BLOCKS = re.compile(r"<!--.*?-->|<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_DELIMITER = re.compile(rf"</?\s*(?:{_NAMES})\b[^<>]*>?", re.IGNORECASE)
_TAG = re.compile(r"</?\s*[a-z][a-z0-9-]*(?:\s[^<>]*)?/?>", re.IGNORECASE)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")  # [text](url) and ![alt](url): keep the text, drop the target
_ENCODED = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")  # a long base64-like blob


def clean_text(text: object, max_chars: int = MAX_CHARS) -> str:
    """Plain, short text with nothing hidden in it. Use it on every string the customer or a note can write."""
    text = unicodedata.normalize("NFKC", str(text)[: max_chars * 4])  # also turns full-width "＜" into "<"
    text = "".join(c for c in text if c in _KEEP or unicodedata.category(c) not in _INVISIBLE)
    text = _BLOCKS.sub(" ", text)
    text = _DELIMITER.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _ENCODED.sub("[encoded text removed]", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


def wrap_untrusted(text: object, tag: str = "customer_message", max_chars: int = MAX_CHARS) -> str:
    """Clean the text and put it between data tags: <customer_message> ... </customer_message>."""
    if tag not in DELIMITER_TAGS:
        raise ValueError(f"unknown data tag: {tag}")
    return f"<{tag}>\n{clean_text(text, max_chars)}\n</{tag}>"


def remove_delimiters(text: object) -> str:
    """Only remove our data tags. For text that is already structured (for example the facts as JSON)."""
    return _DELIMITER.sub(" ", unicodedata.normalize("NFKC", str(text)))


# --- flags ---
_VERB = r"(?:ignore|forget|disregard|override|bypass|bhool|bhul|nazar\s*andaz)"
_TOPIC = r"(?:instructions?|rules?|polic(?:y|ies)|prompts?|guidelines?|limits?|hukm|ahkam|qaid[ae])"
_OTHER = r"(?:neighbou?r|friend|colleague|someone\s+else|somebody\s+else|another\s+customer|other\s+customers?|parosi|dost)"
_ITEM = r"(?:orders?|accounts?|address(?:es)?|phone|details|emails?)"
_EMAIL = r"[\w.+-]+@[\w-]+\.[\w.-]+"

_PATTERNS = {
    name: re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for name, pattern in {
        # "ignore previous instructions", "rules bhool jao", "admin mode", "you are now ..."
        "override_instruction": (
            rf"\b{_VERB}\b.{{0,30}}\b{_TOPIC}\b|\b{_TOPIC}\b.{{0,30}}\b{_VERB}\b"
            r"|\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be)\b"
            r"|\b(?:admin|developer|system|debug|god)\s+mode\b"
            r"|\b(?:system|hidden)\s+prompt\b|\bnew\s+instructions?\b"
        ),
        # "I am the owner", "manager approved by phone", "manager ne approve kar diya"
        "fake_authority": (
            r"\bi\s*(?:am|'m)\s+(?:the\s+)?(?:store\s+)?(?:owner|manager|admin(?:istrator)?|ceo|boss|developer)\b"
            r"|\b(?:main|mein|mai)\b.{0,30}\b(?:owner|manager|admin|malik)\b.{0,10}\b(?:hoon|hun|hu)\b"
            r"|\b(?:manager|owner|admin|boss)\b.{0,40}\b(?:approved?|manzoor|ijazat|allowed?)\b"
            r"|\bapproved\s+by\b|\bpre-?approved\b"
        ),
        # "send me the order of my neighbour", "doosre customer ka order"
        "other_customer_data": (
            rf"\b{_OTHER}\b.{{0,30}}\b{_ITEM}\b|\b{_ITEM}\b.{{0,20}}\b(?:of|for|ka|ki)\b.{{0,20}}\b{_OTHER}\b"
            r"|\ball\s+(?:customers?|orders?)\b"
        ),
        # "email the order list to x@y.com"
        "send_data_out": rf"\b(?:send|email|mail|forward|share|bhej\w*)\b.{{0,80}}{_EMAIL}",
        # "refund 10x", "refund 50,000", "refund -500"
        "amount_manipulation": (
            r"\b(?:\d+\s*x|double|triple|ten\s+times)\b.{0,30}\brefund\b"
            r"|\brefund\b.{0,30}\b(?:\d+\s*x|double|triple|ten\s+times)\b"
            r"|\brefund\b.{0,20}(?:-\s?\d|\b\d{1,3}[, ]\d{3}\b|\b\d{5,}\b)"
        ),
    }.items()
}


def injection_flags(text: object) -> list[str]:
    """Names of the attack signs in the RAW text (before clean_text). An empty list means nothing suspicious.

    Flags: too_long, hidden_characters, markup, encoded_text, override_instruction, fake_authority,
    other_customer_data, send_data_out, amount_manipulation. A flag is a signal for the audit log, never a verdict.
    """
    raw = str(text)
    normal = unicodedata.normalize("NFKC", raw[: MAX_CHARS * 4]).replace("\u2019", "'")
    flags: list[str] = []
    if len(raw) > MAX_CHARS:
        flags.append("too_long")
    if _HIDING.search(normal):
        flags.append("hidden_characters")
    if _BLOCKS.search(normal) or _TAG.search(normal) or _LINK.search(normal):
        flags.append("markup")
    if _ENCODED.search(normal):
        flags.append("encoded_text")
    flags.extend(name for name, pattern in _PATTERNS.items() if pattern.search(normal))
    return flags
