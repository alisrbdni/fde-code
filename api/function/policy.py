"""Forge safety policy (Appendix A.3).

The policy blocks four things, with reasons that can be spoken to the caller:
impersonation of real organisations, collecting payment/ID numbers, hiding
that the agent is an AI, and harming or pressuring callers. Urgency or any
other safety decision is never delegated to an LLM — these are rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List


def _norm(text: Any) -> str:
    value = str(text or "").lower()
    return value.replace("\u2019", "'").replace("\u2018", "'")


IMPERSONATION_BRANDS: List[str] = [
    "irs", "internal revenue", "social security administration", "medicare", "medicaid",
    "fbi", "police department", "sheriff", "uscis", "dmv",
    "amazon", "apple support", "microsoft support", "google support", "paypal", "venmo",
    "zelle", "wells fargo", "chase bank", "bank of america", "citibank", "american express",
    "mastercard", "fedex", "ups", "usps", "dhl", "netflix", "comcast", "verizon", "at&t",
    "t-mobile", "geek squad", "norton", "mcafee", "coinbase", "binance",
]

# A short brand matches only when the business name IS the brand (optionally
# "the ..." and one of these suffixes): "UPS Support" is impersonation,
# "Ups and Downs Yoga" is not.
BUSINESS_SUFFIXES: List[str] = [
    "support", "service", "services", "security", "customer care", "customer service",
    "delivery", "department", "office", "agency", "help desk", "billing", "refund", "refunds",
]

PHRASE_PREFIXES: List[str] = ["we are", "this is", "calling from", "on behalf of"]

SENSITIVE_ITEMS: List[str] = [
    "credit card", "debit card", "card numbers", "cvv", "cvc",
    "payment", "payments", "bank login", "bank password", "bank account",
    "account numbers", "routing numbers", "social security", "ssn",
    "password", "passwords", "pin", "pins", "one-time code", "one-time password",
    "otp", "otps", "verification codes", "gift cards", "gift card",
    "crypto wallet", "crypto wallets", "seed phrases", "seed phrase",
]

COLLECTION_VERBS: List[str] = [
    "ask", "collect", "take", "get", "request", "record", "capture", "confirm",
    "verify", "read back", "note", "gather", "process",
]

COLLECT_NEAR_PATTERNS: List[str] = [
    r"over the phone",
    r"from (the )?callers?",
]

DECEPTION_PATTERNS: List[str] = [
    r"pretend(ing)? to be (a |an )?(human|real person|person)",
    r"(you are|you're|youre) (a |an )?(human|real person)",
    r"(deny|never say|don't say|dont say|do not say|never admit|never tell)( that| to callers)? (you are|you're|youre|being) (a |an )?(ai|bot|robot|automaton)",
    r"say( that)? (you are|you're|youre) (a |an )?(human|real person)",
    r"(hide|conceal)( that| it| from callers)? (you are|you're|youre) (a |an )?(ai|bot|robot)",
    r"(don't|dont|do not|never) (tell|say|mention|reveal)( callers| them| it)? (that )?(you are|you're|youre|being) (a |an )?(ai|bot|robot)",
]

HARM_PATTERNS: List[str] = [
    r"\bthreaten(s|ing)?\b",
    r"\barrest(s|ing)?\b",
    r"\blawsuit against\b",
    r"\bdebt collect(or|ion|ions|ing)?\b",
    r"\bact now or\b",
    r"\bfinal notice\b",
    r"(?<![a-z])diagnos",
    r"\bprescrib(e|es|ing)\b",
    r"instead of (calling )?911",
    r"\bemergency (medical )?advice\b",
    r"\b(vote|voting) for\b",
    r"\brobocalls?\b",
    r"\bcold-?calls?ing?\b",
    r"\bcall (a|this|my) list\b",
]

REASON_IMPERSONATION = "it would present itself as {name}, a real organisation you don't own"
REASON_SENSITIVE = "it would ask callers for payment, banking, password or ID numbers"
REASON_DECEPTION = "it would hide that it is an AI"
REASON_HARM = (
    "it would threaten callers, give medical or emergency advice, persuade voters, or cold-call people"
)


@dataclass
class Verdict:
    allowed: bool = True
    reasons: List[str] = field(default_factory=list)


def _word(pattern: str) -> str:
    return r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"


def _verb_patterns() -> List[re.Pattern]:
    patterns = []
    for verb in COLLECTION_VERBS:
        stem = verb if verb.endswith(("back",)) else verb
        if " " in stem:
            patterns.append(re.compile(_word(re.escape(stem))))
            continue
        patterns.append(re.compile(_word(re.escape(stem) + r"(s|ed|ing)?")))
    return patterns


_VERBS = _verb_patterns()


def _compile_items(items: List[str]) -> List[re.Pattern]:
    core_by_item: Dict[str, str] = {
        "social security": r"social security( numbers?)?",
        "pin": r"pins?",
        "otp": r"otps?",
        "password": r"passwords?",
        "payment": r"payments?",
        "account numbers": r"account numbers?",
        "routing numbers": r"routing numbers?",
        "verification codes": r"verification codes?",
        "one-time code": r"one-?time codes?",
        "one-time password": r"one-?time passwords?",
        "gift cards": r"gift cards?",
        "gift card": r"gift cards?",
        "crypto wallets": r"crypto wallets?",
        "crypto wallet": r"crypto wallets?",
        "seed phrases": r"seed phrases?",
        "seed phrase": r"seed phrases?",
        "credit card": r"credit card",
        "debit card": r"debit cards?",
        "card numbers": r"card numbers?",
        "cvv": r"cvv",
        "cvc": r"cvc",
        "bank account": r"bank account",
        "bank login": r"bank logins?",
        "bank password": r"bank password",
    }
    out: List[re.Pattern] = []
    for item in items:
        core = core_by_item.get(item)
        if core is None:
            base = item[:-1] if (item.endswith("s") and len(item) > 3 and not item.endswith("ss")) else item
            core = re.escape(base) + ("s?" if base != item else "")
        out.append(re.compile(_word(core)))
    return out


_ITEMS = _compile_items(SENSITIVE_ITEMS)


def _texts(fields: Dict[str, Any]) -> List[str]:
    """Every user-authored text field, normalised."""
    out: List[str] = []
    for name in ("business_name", "agent_name", "purpose", "greeting", "audience", "hours", "tone"):
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            out.append(_norm(value))
    for name in ("tasks", "facts"):
        value = fields.get(name)
        if isinstance(value, list):
            out.extend(_norm(v) for v in value if isinstance(v, str))
    return out


def _short_business_is_brand(business: str) -> bool:
    tokens = business.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    suffixes = set(BUSINESS_SUFFIXES)
    while len(tokens) > 1 and tokens[-1] in suffixes:
        tokens = tokens[:-1]
    joined = " ".join(tokens)
    for brand in IMPERSONATION_BRANDS:
        if len(brand) <= 4 and joined == brand:
            return True
    return False


def _impersonation(business: str, texts: List[str]) -> bool:
    # The business name IS a (short) brand + optional suffixes.
    if _short_business_is_brand(business):
        return True
    # Long brands appear as words inside the business name.
    for brand in IMPERSONATION_BRANDS:
        if len(brand) > 4 and re.search(_word(re.escape(brand)), business):
            return True
    # "we are / this is / calling from / on behalf of <brand>"
    for text in texts:
        for prefix in PHRASE_PREFIXES:
            at = text.find(prefix)
            while at != -1:
                rest = text[at + len(prefix): at + len(prefix) + 40]
                for brand in IMPERSONATION_BRANDS:
                    if re.search(_word(r"(the )?" + re.escape(brand)), rest):
                        return True
                at = text.find(prefix, at + 1)
    return False


def _collects_sensitive(texts: List[str]) -> bool:
    for text in texts:
        for item_re in _ITEMS:
            for match in item_re.finditer(text):
                start = max(0, match.start() - 50)
                before = text[start: match.start()]
                for verb_re in _VERBS:
                    if verb_re.search(before):
                        return True
                after = text[match.end(): match.end() + 30]
                for near_re in COLLECT_NEAR_PATTERNS:
                    if re.search(near_re, after):
                        return True
    return False


def check(fields: Dict[str, Any]) -> Verdict:
    """Rule-based verdict on a draft. Never an LLM."""
    texts = _texts(fields)
    business = next((t for t in texts if t == _norm(fields.get("business_name"))), "")
    all_text = " ; ".join(texts)
    reasons: List[str] = []

    if _impersonation(business, texts):
        reasons.append(REASON_IMPERSONATION.format(name=fields.get("business_name") or "another company"))
    if _collects_sensitive(texts):
        reasons.append(REASON_SENSITIVE)
    if any(re.search(pattern, " ; ".join(text for text in texts)) for pattern in DECEPTION_PATTERNS):
        reasons.append(REASON_DECEPTION)
    if any(re.search(pattern, all_text) for pattern in HARM_PATTERNS):
        reasons.append(REASON_HARM)

    return Verdict(allowed=not reasons, reasons=reasons)
