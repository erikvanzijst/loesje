"""Word counting and search normalization, shared by pgload.py and server.py."""

import difflib
import re
import unicodedata

# A word is a run of letters/digits, optionally joined by hyphens or apostrophes
# ("heren-wc’s", "’s" and "1983" are one word each). Slashes, dashes and other
# punctuation separate words but are not words themselves.
# Dotted abbreviations ("I.D.", "a.e.p.") and "m/v" also count as one word.
WORD_RE = re.compile(r"(?:[^\W_]\.){2,}|\b[mMvV]/[vVmM]\b|[^\W_]+(?:[-'’‘][^\W_]+)*|[-'’‘][^\W_]+")


def count_words(s):
    return len(WORD_RE.findall(s or ""))


def fold(s):
    """Lowercase and strip accents, so "Hygiëne" and "hygiene" match."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def search_terms(q):
    """Folded alphanumeric terms of a user query."""
    return re.findall(r"[^\W_]+", fold(q))


# Credits that some CMS titles carry after the slogan: "– oom Herman", "-de opa van-",
# ", tante Rie", "het broertje van", "de vader van".
_KIN = (r"(?:broertje|zusje|broer|zus|opa|oma|vader|moeder|tante|oom|ome|neef|nicht|"
        r"buurman|buurvrouw|zoon|dochter|kleinzoon|kleindochter|vriendje|vriendinnetje)")
_CREDIT_RES = [
    re.compile(r"\s+[–—-]\s+(?:\S+\s+){0,2}\S+\s+van\s+loesje$", re.I),  # – Legofilius Peppeldam van Loesje
    re.compile(r"\s*-[^-/]{2,30}-\s*$"),                                  # -de opa van-
    re.compile(r"\s+[–—]\s+[^–—/]{2,30}$"),                               # – oom Herman
    re.compile(r",\s*" + _KIN + r"\s+\S+$", re.I),                        # , tante Rie
    re.compile(r"\s+(?:neef\s+karel|tante\s+rie|oom\s+herman|ome\s+henk)(?:\s+van)?$", re.I),  # recurring characters
    re.compile(r"\s+(?:(?:het|de|een|mijn)\s+)?" + _KIN + r"(?:\s+\S+)?\s+van$", re.I),  # het broertje van
    re.compile(r"\s+(?:\S+\s+)?\S+\s+van$", re.I),  # any other credit: "de kapper van" (2 words max)
]


def strip_credit(title):
    t = (title or "").rstrip(" .!;,")  # "…de opa van." ends in a period on some titles
    for rx in _CREDIT_RES:
        t = rx.sub("", t)
    return t


def fold_words(s):
    return [fold(w) for w in WORD_RE.findall(s or "")]


def letters(s):
    return "".join(c for c in fold(s) if c.isalnum())


def clean_title(title):
    """Title without credits or bracketed notes ("(tekening)", "[tekening kroon]")."""
    t = re.sub(r"\s*[\[(][^\])]*[\])]\s*", " ", title or "").strip()
    return strip_credit(t).strip()


PLACEHOLDER_TITLES = {"meeschrijftekst", "typ hier de volledige tekst van de poster"}


def _matched(a, b):
    """Number of characters of a that occur, in order, in b."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(blk.size for blk in sm.get_matching_blocks())


def decide_word_count(text, title):
    """Pick the text a poster's word count is based on.

    Returns (word_count, source) where source is 'title' or 'text' (OCR).

    The CMS title, typed by Loesje's editors, is the primary source: it is usually
    right, while OCR struggles with curved, tilted and sideways text. OCR is the
    cross-check that repairs the title's known problems:
    - credits appended to the title ("– oom Herman", "het broertje van") are cut,
      unless the OCR shows those words are part of the slogan;
    - an empty or placeholder title ("meeschrijftekst") -> use the OCR text;
    - a title that misses words: the OCR contains all of the title plus 3+ more
      letters ("ik vind profa veel gevaarlijker") -> use the OCR text.
    """
    # Editorial notes such as "(tekening)" or "[foto van Bea]", but not "(m/v)".
    full = re.sub(r"\s*[\[(](?:[^\])]*\s[^\])]*|tekening|foto)[\])]\s*", " ", title or "", flags=re.I).strip()
    t = strip_credit(full).strip()
    has_text = bool(text and text.strip())
    ol = letters(text) if has_text else ""
    tail = letters(full[len(t):])
    if tail and ol.endswith(tail):  # the "credit" is actually printed as part of the slogan
        t = full
    if not fold_words(t) or fold(t) in PLACEHOLDER_TITLES:
        return (count_words(text), "text") if has_text else (0, "title")
    tl = letters(t)
    if has_text and _matched(tl, ol) >= 0.95 * len(tl) and len(ol) - len(tl) >= 3:
        return count_words(text), "text"
    return count_words(t), "title"
