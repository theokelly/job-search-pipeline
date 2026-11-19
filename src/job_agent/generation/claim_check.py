"""Deterministic grounding checks for resume-recommendation edits.

The generation model occasionally upgrades claims while tailoring — most
dangerously hard numbers: a digit survives paraphrase while its unit mutates
("4 functions" -> "4 use cases"), or a percentage appears from nowhere
("100% adoption"). Numbers are mechanically checkable, so this module checks
them mechanically: every numeral in a suggested edit must exist in the source
corpus with a matching unit context, and a rephrase may not change the value
attached to a unit its original used.

Semantic upgrades (scale language, invented negations) carry no numerals to
catch; those are the LLM grounding check's job (generation.truthfulness).

Fail-open by design: an empty corpus (no source materials loaded) grounds
nothing and therefore blocks nothing.
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

_TOKEN_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:%|[KMB]\b|million\b|billion\b|thousand\b)?"
    r"|[A-Za-z][A-Za-z\-']*",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(
    r"^(\d[\d,]*(?:\.\d+)?)\s*(%|[KMB]|million|billion|thousand)?$",
    re.IGNORECASE,
)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "by", "at", "from", "as", "per", "across", "over", "under", "into",
    "than", "then", "was", "were", "are", "is", "be", "been", "it", "its",
    "their", "his", "her", "our", "your", "my", "we", "i", "he", "she", "they",
}
_SCALES = {"k": 1e3, "m": 1e6, "b": 1e9,
           "thousand": 1e3, "million": 1e6, "billion": 1e9}


def _norm_token(tok: str) -> str:
    t = tok.lower().strip("'- ")
    if len(t) > 4 and t.endswith("ies"):
        t = t[:-3] + "y"
    elif len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        t = t[:-1]
    return t


def _value_key(raw: str) -> tuple[str, bool]:
    """Canonical key for a numeral token: (value_key, is_year)."""
    m = _NUMBER_RE.match(raw.strip())
    if not m:
        return raw.strip(), False
    digits, suffix = m.group(1), (m.group(2) or "").lower()
    is_year = bool(re.fullmatch(r"(19|20)\d{2}", digits)) and not suffix
    value = float(digits.replace(",", ""))
    if suffix in _SCALES:
        value *= _SCALES[suffix]
    key = str(int(value)) if value == int(value) else str(value)
    if suffix == "%":
        key += "%"
    return key, is_year


@dataclass
class Claim:
    raw: str
    key: str
    is_year: bool
    unit_tokens: set = field(default_factory=set)
    primary_unit: str | None = None


def extract_claims(text: str) -> list[Claim]:
    """Every numeral in `text`, with its surrounding unit context."""
    tokens = list(_TOKEN_RE.finditer(text or ""))
    claims: list[Claim] = []
    for i, tok in enumerate(tokens):
        raw = tok.group(0)
        if not raw[0].isdigit():
            continue
        key, is_year = _value_key(raw)
        before = [t for t in tokens[:i] if not t.group(0)[0].isdigit()]
        after = [t for t in tokens[i + 1:] if not t.group(0)[0].isdigit()]
        window = before[-2:] + after[:2]
        unit_tokens = {
            _norm_token(t.group(0)) for t in window
            if _norm_token(t.group(0)) not in _STOPWORDS
        }
        primary = None
        for t in after:
            n = _norm_token(t.group(0))
            if n not in _STOPWORDS:
                primary = n
                break
        if primary is None:
            for t in reversed(before):
                n = _norm_token(t.group(0))
                if n not in _STOPWORDS:
                    primary = n
                    break
        claims.append(Claim(raw=raw.strip(), key=key, is_year=is_year,
                            unit_tokens=unit_tokens, primary_unit=primary))
    return claims


@dataclass
class Corpus:
    index: dict = field(default_factory=dict)   # value_key -> set of context tokens
    paragraphs: list = field(default_factory=list)  # normalized source lines


def build_corpus(texts) -> Corpus:
    corpus = Corpus()
    for text in texts or []:
        if not text:
            continue
        for claim in extract_claims(text):
            corpus.index.setdefault(claim.key, set()).update(claim.unit_tokens)
        for line in text.splitlines():
            line = " ".join(line.split()).lower()
            if len(line) >= 10:
                corpus.paragraphs.append(line)
    return corpus


def check_suggested(suggested: str, corpus: Corpus,
                    original: str | None = None) -> list[str]:
    """Flags for one suggested text. Empty list = grounded."""
    if not corpus.index:
        return []
    flags: list[str] = []
    for claim in extract_claims(suggested):
        if claim.is_year:
            if claim.key not in corpus.index:
                flags.append(f"'{claim.raw}' does not appear in the source materials")
            continue
        known = corpus.index.get(claim.key)
        if known is None:
            flags.append(f"'{claim.raw}' does not appear in the source materials")
        elif claim.unit_tokens and not (claim.unit_tokens & known):
            flags.append(
                f"'{claim.raw}' appears in the sources but not with this unit")
    if original:
        orig = {c.primary_unit: c for c in extract_claims(original) if c.primary_unit}
        sugg = {c.primary_unit: c for c in extract_claims(suggested) if c.primary_unit}
        for unit, o in orig.items():
            s = sugg.get(unit)
            if s and s.key != o.key:
                flags.append(f"'{unit}' changed from {o.raw} to {s.raw}")
    return flags


def _quote_grounded(quote: str, corpus: Corpus) -> bool:
    if check_suggested(quote, corpus):
        return False
    q = " ".join(quote.split()).lower()
    if not q:
        return False
    return any(q in p or SequenceMatcher(None, q, p).ratio() >= 0.8
               for p in corpus.paragraphs)


def check_edit(edit: dict, corpus: Corpus, require_quote: bool = False) -> list[str]:
    """Flags for one recommendation edit. Reorders are never checked.

    require_quote: brand-new bullets (original '<UNKNOWN>') must then carry a
    source_quote that is itself grounded in the corpus. The tailor backstop
    passes False — the quote was verified at generation time and is not
    persisted in the recommendation markdown.
    """
    if edit.get("operation", "replace") == "reorder":
        return []
    original = (edit.get("original") or "").strip()
    is_insert = not original or original == "<UNKNOWN>"
    flags = check_suggested(
        edit.get("suggested", ""), corpus,
        original=None if is_insert else original)
    quote = (edit.get("source_quote") or "").strip()
    if is_insert and require_quote and not quote:
        flags.append("new bullet has no source_quote")
    elif quote and not _quote_grounded(quote, corpus):
        flags.append("source_quote not found in the source materials")
    return flags
