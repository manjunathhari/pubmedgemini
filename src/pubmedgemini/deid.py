"""The patient-details check: Incisive's local identifier filter, ported rule for rule.

It runs before a question can leave the app (owner decision of 11 Nov 2026). A question with
any finding is refused whole: nothing is stored and nothing is sent to Google, PubMed or
Europe PMC. Nothing is redacted or rewritten; a question with no finding goes on unchanged
(Incisive `ask.rs` `run_ask`: findings block; otherwise the question is stored as given).

Source: Incisive commit 12630f7, `crates/incisive-app/src/deid.rs` (whole file) and `ask.rs`
`BLOCKED_MESSAGE`, `blocked_message`. Rule by rule:

| Category | What it catches | Incisive symbol |
|---|---|---|
| `aadhaar` | 12 digits, first 2 to 9, in 4-4-4 groups or run together, not part of a longer number | `RULES` (Aadhaar), `standalone_number`, `inside_longer_number` |
| `pan` | AAAAA9999A, or any case after a `PAN` label | `RULES` (Pan) |
| `phone` | Indian mobiles (+91 / 91 / 0, 10 digits from 6 to 9) unless a lab word, a range or a unit says it is a count; any grouping after a phone label; landlines with an STD code | `RULES` (Phone x4), `phone_check`, `labelled_phone`, `looks_like_range`, `phone_label_before` |
| `email` | an address with `@` or full-width `＠` | `RULES` (Email) |
| `calendar_date` | d/m/y, y-m-d, `12 March 2024`, `12th Mar`, `March 12, 2024`, `3rd May` | `RULES` (CalendarDate x5) |
| `hospital_id` | UHID, MRN, IPD, Patient ID, MR/IP/OP/Reg/Hosp no. followed by an id with a digit; `IP 4471` unless a unit follows | `RULES` (HospitalId x2), `no_unit_after` |
| `person_name` | a title + capitalised word (eponyms pass in procedure context); all-caps titles; a patient-context word + name; a `Name:` label; s/o, d/o, w/o, c/o, h/o, son/daughter/wife/husband of + name, with Incisive's clinical-phrase and age rules; extended over up to two more name words | `RULES` (PersonName x5), `name_check_titled`, `name_check_caps_title`, `name_check_context`, `name_check_label`, `name_label_ok`, `relation_check`, `diagnosis_phrase`, `age_follows`, `comma_age_follows`, `extend_name`, the word lists |
| `address` | door / flat / house / plot numbers, `No. 45, MG Road`, sectors, `#12 ... road`, cross and main roads, `45 MG Road`, `Koramangala 5th Block`, `...nagar` or `... Layout` near a digit, and 6-digit PIN codes with an address cue | `RULES` (Address x10), `pin_check`, `pin_label_before`, `address_label_before`, `PLACE_BEFORE`, `count_noun`, `locality_check`, `numbered_label_ok` |
| `unusual_characters` | any character outside the allowlist: other scripts, hidden or look-alike characters, emoji, Roman numeral characters, en dashes and similar between digits | `unusual_characters`, `allowed_letter`, `allowed_symbol`, `contextual`, `unit_mark`, `is_mark` |

Every word list, pattern and message is copied from Incisive; the patterns are Incisive's own
text, turned into Python's syntax mechanically by `_pat`. Where Python's `re` would read a
pattern differently from Rust's `regex`, the regexes run on a same-length shadow of the text
(`_shadow`) and every check reads the original:

* Rust's `\\b` treats combining marks, connector punctuation and joiners as word characters and
  superscript digits and fractions (category No) as not; Python's is the other way round.
* Python's case-insensitive `[a-z]` also matches `İ` and `ı`; Rust's matches only `ſ` and `K`.
* Python's `\\s` also matches U+001C to U+001F; Rust's (Unicode White_Space) does not.
* Python's `$` also matches before a final newline: Incisive's `$` becomes `\\Z`.

Offsets here are character indices into the question (Incisive's are UTF-8 bytes); every
distance Incisive measures in bytes is measured in bytes here too. Limits, as Incisive states
them (`deid.rs` module doc): the check is a set of patterns. It cannot recognise every detail
that identifies a patient (a lower-case or non-Latin name, a rare place, a story that fits one
person), so the privacy line on the page stays.
"""

import bisect
import re
import string
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache

KINDS = (
    "aadhaar",
    "pan",
    "phone",
    "email",
    "calendar_date",
    "hospital_id",
    "person_name",
    "address",
    "unusual_characters",
)

_LABELS = {
    "aadhaar": "Aadhaar number",
    "pan": "PAN number",
    "phone": "phone number",
    "email": "email address",
    "calendar_date": "calendar date",
    "hospital_id": "hospital or patient ID",
    "person_name": "person's name",
    "address": "street address or PIN code",
    "unusual_characters": "hidden or unusual characters",
}

# ask.rs BLOCKED_MESSAGE, with this app's name where Incisive names itself (as the plan did
# for the long-question message).
BLOCKED_MESSAGE = (
    "pubmedgemini found details that could identify a patient. Please remove them and resend "
    "your question."
)


def describe(kind: str) -> str:
    """User-facing label for a finding kind (deid.rs `describe`)."""
    return _LABELS[kind]


def hint(kind: str) -> str | None:
    """Extra advice where removing the span is not enough (deid.rs `hint`)."""
    if kind == "unusual_characters":
        return ("please retype the question without copy-pasted special characters "
                "(use plain hyphens, colons and spaces)")
    return None


def blocked_message(kinds: Iterable[str]) -> str:
    """ask.rs `blocked_message`: the message, plus each distinct kind's hint as a sentence."""
    message = BLOCKED_MESSAGE
    seen: list[str] = []
    for k in kinds:
        h = hint(k)
        if h is None or h in seen:
            continue
        seen.append(h)
        message += " " + h[0].upper() + h[1:] + "."
    return message


@dataclass(frozen=True)
class Finding:
    """A blocked span: `start` and `end` are character indices into the scanned text."""

    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class BlockedIdentifiers:
    """A refused question: the categories found, each with how many times, in Incisive's order,
    and the message to show. Never the matched text or where it is."""

    categories: tuple[tuple[str, int], ...]
    message: str

    @property
    def labels(self) -> tuple[str, ...]:
        """The user-facing label of each category, in the same order."""
        return tuple(describe(k) for k, _ in self.categories)

    @property
    def total(self) -> int:
        return sum(n for _, n in self.categories)


def check_question(text: str) -> BlockedIdentifiers | None:
    """None when the question may go on as it is; else what blocked it."""
    findings = scan(text)
    if not findings:
        return None
    counts = Counter(f.kind for f in findings)
    categories = tuple((k, counts[k]) for k in KINDS if counts[k])
    return BlockedIdentifiers(categories, blocked_message(f.kind for f in findings))


# ---------- Rust's character classes, in Python ----------

# Unicode White_Space: Rust's `\s`, `char::is_whitespace`, `trim`, `split_whitespace`.
WHITE_SPACE = (
    "\t\n\x0b\x0c\r \x85\xa0\U00001680\U00002000\U00002001\U00002002\U00002003\U00002004"
    "\U00002005\U00002006\U00002007\U00002008\U00002009\U0000200a\U00002028\U00002029"
    "\U0000202f\U0000205f\U00003000"
)
_WS = frozenset(WHITE_SPACE)
_WS_SPLIT = re.compile("[" + re.escape(WHITE_SPACE) + "]+")
_PUNCT = string.punctuation  # char::is_ascii_punctuation


def _split_ws(s: str) -> list[str]:
    return [w for w in _WS_SPLIT.split(s) if w]


def _last_tokens(text: str, s: int, n: int) -> list[str]:
    """`text[..s].split_whitespace().rev().take(n)`, walked back from `s` (last token
    first), so a match costs its neighbourhood, not the whole text."""
    out: list[str] = []
    i = s
    while len(out) < n:
        while i > 0 and text[i - 1] in _WS:
            i -= 1
        if i == 0:
            break
        j = i
        while j > 0 and text[j - 1] not in _WS:
            j -= 1
        out.append(text[j:i])
        i = j
    return out


def _trim(s: str) -> str:
    return s.strip(WHITE_SPACE)


def _trim_start(s: str) -> str:
    return s.lstrip(WHITE_SPACE)


def _is_alphabetic(c: str) -> bool:
    """Rust `char::is_alphabetic` (Alphabetic): letters and letter numbers. Not checked:
    Other_Alphabetic marks such as U+0345, which Rust counts and this does not."""
    return c.isalpha() or unicodedata.category(c) == "Nl"


def _is_numeric(c: str) -> bool:
    """Rust `char::is_numeric`: Nd, Nl, No (Python's isnumeric also takes some CJK letters)."""
    return unicodedata.category(c) in ("Nd", "Nl", "No")


def _is_alphanumeric(c: str) -> bool:
    return _is_alphabetic(c) or _is_numeric(c)


def _ascii_lower(s: str) -> str:
    return "".join(c.lower() if c.isascii() else c for c in s)


def _eq_ascii_ci(a: str, b: str) -> bool:
    """Rust `eq_ignore_ascii_case` (`b` is ASCII here)."""
    return len(a) == len(b) and _ascii_lower(a) == b


_NONWORD = "\U0000fffe"  # a non-word, non-space character no pattern names
_WORD = "\U00004e00"  # a word character no pattern names
# Other_Alphabetic symbols that Rust's \w counts as letters (circled and squared letters).
_OTHER_ALPHA = ((0x24B6, 0x24E9), (0x1F130, 0x1F149), (0x1F150, 0x1F169), (0x1F170, 0x1F189))


@cache
def _shadow_char(c: str) -> str:
    o = ord(c)
    if o < 0x80:
        return _NONWORD if 0x1C <= o <= 0x1F else c
    cat = unicodedata.category(c)
    if cat == "No":
        return _NONWORD
    if (cat[0] == "M" or cat == "Pc" or c in "\U0000200c\U0000200d"
            or any(a <= o <= b for a, b in _OTHER_ALPHA)):
        return _WORD
    if c in "\U00000130\U00000131":
        return _WORD
    return c


def _shadow(text: str) -> str:
    """The text with the characters Python's `re` and Rust's `regex` read differently swapped
    for stand-ins both read the same way (see the module doc). Same length as `text`."""
    if text.isascii() and not any("\x1c" <= c <= "\x1f" for c in text):
        return text
    return "".join(_shadow_char(c) for c in text)


_RUST_HEX = re.compile(r"\\x\{([0-9A-Fa-f]+)\}")


def _py(p: str) -> str:
    """Rust regex syntax to Python's: `\\x{...}` escapes, and `$` (end of text) as `\\Z`."""
    p = _RUST_HEX.sub(lambda m: f"\\U{int(m.group(1), 16):08x}", p)
    return re.sub(r"(?<!\\)\$", r"\\Z", p)


# Pattern placeholders (deid.rs `H`, `A`, `SP`, `DS`, `DAY`, `MON`, expanded by `pat`).
H = r"[ \x{A0}\x{202F}\x{2009}\-\x{2013}\x{2014}]"
A = r"[ \x{A0}\x{202F}\x{2009}\-\x{2013}\x{2014}.]{0,2}"
SP = r"[ \x{A0}\x{202F}\x{2009}]"
DS = r"[/.\-\x{2013}\x{2014}]"
DAY = r"(?:0?[1-9]|[12][0-9]|3[01])"
MON = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?|"
       "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")


def pat(p: str) -> str:
    """deid.rs `pat`: expand placeholders and force ASCII digits; then Python's syntax."""
    p = (p.replace("$DAY", DAY).replace("$MON", MON).replace("$DS", DS).replace("$SP", SP)
         .replace("$H", H).replace("$A", A).replace(r"\d", "[0-9]"))
    return _py(p)


# ---------- the text, with byte offsets ----------


class _Text:
    """The scanned text, its shadow and the UTF-8 byte offset of every character (plus the
    end), so Incisive's byte windows can be measured exactly."""

    def __init__(self, s: str) -> None:
        self.s = s
        self.sh = _shadow(s)
        b = [0]
        for c in s:
            b.append(b[-1] + (1 if c < "\x80" else len(c.encode("utf-8", "surrogatepass"))))
        self.b = b

    def ahead(self, i: int, nbytes: int) -> int:
        """`ceil_boundary(text, byte(i) + n)` as a character index."""
        return min(bisect.bisect_left(self.b, self.b[i] + nbytes), len(self.s))

    def behind(self, i: int, nbytes: int) -> int:
        """`floor_boundary(text, byte(i) - n)` as a character index."""
        return bisect.bisect_right(self.b, max(0, self.b[i] - nbytes)) - 1


@dataclass
class _M:
    """A regex match on the shadow, read against the original text."""

    t: _Text
    m: re.Match

    @property
    def start(self) -> int:
        return self.m.start()

    @property
    def end(self) -> int:
        return self.m.end()

    def group(self, name: str) -> str | None:
        try:
            a, b = self.m.span(name)
        except IndexError:
            return None
        return None if a < 0 else self.t.s[a:b]

    def group_start(self, name: str) -> int | None:
        a = self.m.start(name)
        return None if a < 0 else a


# Word lists, copied from deid.rs (each `const NAME: &[&str]`), in Incisive's order.

LAB_WORDS = (
    "platelets", "platelet", "plt", "plts", "count", "wbc", "tlc", "iu", "units", "cells", "rs",
    "inr", "\U000020b9", "patients", "cases", "total", "size", "n", "cost", "rupees", "ferritin",
    "afp", "cea", "ca", "dimer", "d-dimer", "load", "copies", "creatinine", "glucose", "lipase",
    "amylase", "ldh", "crp", "esr", "wcc", "neutrophils", "lymphocytes", "leucocytes",
    "leukocytes",
)
UNIT_PREFIXES = (
    "/\U000000b5", "/\U000003bc", "/ul", "/cumm", "/mm", "/cu", "cells", "cell", "iu", "units",
    "unit", "mg", "ml", "mm", "cc", "mcg", "ug", "ng", "pg", "cu", "per", "%", "\U000000b5",
    "\U000003bc", "copies", "patients", "cases", "rupees",
)
PHONE_VETO_WORDS = (
    "platelets", "platelet", "plt", "plts", "count", "wbc", "tlc", "iu", "cells", "cumm",
    "ferritin", "afp", "cea", "dimer", "d-dimer", "load", "copies", "esr", "crp", "ldh", "lipase",
    "amylase", "creatinine", "glucose", "wcc", "neutrophils", "lymphocytes", "leucocytes",
    "leukocytes",
)
PHONE_UNITS = (
    "/cumm", "/mm3", "/\U000000b5L", "/\U000000b5l", "/uL", "/ul", "/cu", "cells", "copies",
)
PHONE_LABELS = (
    "ph", "phone", "mob", "mobile", "call", "contact", "tel", "whatsapp", "attender", "cell",
)
UNITS_AFTER_NUMBER = (
    "mmhg", "mcg", "mm", "cm", "mg", "ml", "kg", "iu",
)
PIN_LABELS = (
    "pin", "pincode", "pin code", "pin-code", "postcode", "post code", "postal code", "zip",
    "zipcode",
)
COUNT_NOUNS = (
    "patients", "procedures", "admissions", "beneficiaries", "cases", "gastrectomies",
    "cholecystectomies", "resections", "operations", "subjects", "participants", "copies", "cells",
    "surgeries", "deaths", "births", "people", "individuals", "adults", "children", "samples",
    "specimens", "records", "encounters", "discharges",
)
EPONYMS = (
    "hartmann", "whipple", "nissen", "crohn", "roux", "child", "pugh", "billroth", "kocher",
    "pringle", "calot", "lichtenstein", "stoppa", "miles", "dukes", "clavien", "dindo", "heller",
    "kasai", "toupet", "dor", "rives", "shouldice", "bassini", "hasson", "veress", "puestow",
    "frey", "beger", "warshaw", "longmire", "mikulicz", "soave", "duhamel", "swenson", "barrett",
    "lynch", "trendelenburg", "glasgow", "ranson", "balthazar", "atlanta", "tokyo", "bismuth",
    "strasberg", "couinaud", "cattell", "braasch", "hinchey", "parks", "ivor", "lewis", "appleby",
    "cantlie", "gerota", "denonvilliers", "waldeyer", "toldt", "murphy", "courvoisier",
    "boerhaave", "mallory", "zollinger", "charlson", "caprini", "kaplan", "cox", "gleason",
    "breslow", "fontaine", "rutherford", "bosniak", "klatskin", "mirizzi", "meckel", "budd",
    "chiari", "caroli", "wilms", "zenker", "morgagni", "bochdalek", "amyand", "littre", "richter",
    "spigelian", "krukenberg", "virchow", "rovsing", "mcburney", "pfannenstiel", "fournier",
    "ogilvie", "ladd", "bouveret", "frantz", "vater", "oddi", "treitz", "morison", "winslow",
    "peutz", "jeghers", "dieulafoy", "menetrier", "hodgkin", "burkitt", "kaposi", "hashimoto",
    "paget",
)
PROC_WORDS = (
    "procedure", "procedures", "pouch", "operation", "sign", "fundoplication", "repair", "triad",
    "classification", "score", "criteria", "colostomy", "reversal", "anastomosis", "resection",
    "myotomy", "manoeuvre", "maneuver", "incision", "triangle", "syndrome", "test", "point",
    "ligament", "node", "nodes", "line", "technique", "disease", "stage", "staging",
)
TITLE_COMMON = (
    "excel", "class", "classes", "rate", "rates", "plan", "list", "key", "mind", "thesis",
    "degree", "regimen", "blood", "controlled",
)
NOT_NAMES = (
    "the", "this", "that", "these", "those", "and", "with", "without", "for", "from", "after",
    "before", "has", "had", "have", "was", "were", "who", "which", "male", "female", "man",
    "woman", "boy", "girl", "post", "pre", "day", "days", "week", "weeks", "known", "presented",
    "presenting", "presents", "underwent", "undergoing", "undergone", "information", "info", "age",
    "aged", "history", "safety", "selection", "outcome", "outcomes", "consent", "satisfaction",
    "education", "characteristics", "demographics", "management", "factors", "pathway", "care",
    "related", "reported", "positioning", "preparation", "recovery", "details", "data", "name",
    "number", "record", "records", "notes", "note", "report", "summary", "discharge", "admission",
    "profile", "population", "cohort", "group", "experience", "perspective", "perspectives",
    "preference", "preferences", "hct", "wbc", "plt", "cea", "bmi", "asa", "pod", "ecog", "hba",
    "inr", "ast", "alt", "alp", "ggt", "crp", "developed", "required", "received", "tolerated",
    "remained", "became", "complained", "denied", "reports", "refused", "agreed", "died",
    "recovered", "improved", "deteriorated", "doing", "stable", "unstable", "febrile", "afebrile",
    "jaundiced", "icteric", "pale", "obese", "diabetic", "hypertensive", "allergic", "positive",
    "negative", "normal", "abnormal", "blood", "controlled", "excel", "class", "rate", "wise",
    "abdominal", "pain", "fever", "fevers", "vomiting", "nausea", "bleeding", "jaundice",
    "swelling", "breathlessness", "cough", "loose", "constipation", "weight", "loss", "chest",
    "headache", "dysphagia", "diarrhoea", "diarrhea", "burning", "difficulty", "lump", "mass",
    "distension", "giddiness", "weakness", "fatigue", "itching", "dyspnea", "dyspnoea", "abdomen",
    "right", "left", "upper", "lower", "epigastric", "generalised", "generalized", "recurrent",
    "persistent", "progressive", "acute", "chronic", "decreased", "reduced", "increased", "black",
    "yellowish", "yellow", "bloating", "anorexia", "palpitations", "syncope", "haematuria",
    "hematuria", "melena", "diabetes", "hypertension", "comorbidities", "complications",
    "symptoms", "evidence", "features", "signs", "any",
)
NON_PERSON_NAME_QUALIFIERS = (
    "brand", "drug", "trade", "generic", "proprietary", "trial", "study", "device", "product",
    "implant", "mesh", "instrument", "stapler", "suture", "procedure", "surgery", "operation",
    "test", "investigation", "medicine", "medication", "molecule", "compound", "vaccine",
    "antibiotic", "scheme", "programme", "program", "protocol", "file", "kit", "score", "scale",
    "classification", "technique", "disease", "diagnosis", "syndrome", "gene", "organism",
    "software", "journal",
)
NAME_PLACEHOLDERS = (
    "unknown", "withheld", "anonymous", "anonymised", "anonymized", "redacted", "nil", "none",
    "not",
)
STRONG_SUFFIXES = (
    "itis", "oma", "osis", "ectomy", "emia", "aemia", "uria", "rrhoea", "rrhea", "rrhage",
    "rrhoids", "ation", "graphy", "scopy", "pathy", "plasty", "otomy", "ostomy", "ancy", "ncy",
    "sis", "gia", "ism", "cele",
)
NOT_NAMES_2 = (
    "mucocele", "abscess", "ascites", "fistula", "volvulus", "leak", "obstruction", "perforation",
    "malignancy", "grade", "small", "bowel", "wall", "thickening", "hirschsprung", "injury",
    "conversion", "drain", "contrast", "mild", "pain", "vomiting", "acute", "stricture", "polyp",
    "tumour", "tumor", "lesion", "cyst", "calculus", "stone", "collection", "seen", "admitted",
    "operated", "referred", "scheduled", "advised", "discharged", "planned", "brought", "came",
    "dyspepsia", "heartburn", "haematemesis", "hematemesis", "malena", "pruritus", "reflux",
    "regurgitation", "obstipation", "hernia", "haemorrhoids", "piles", "severe", "moderate",
    "sudden", "intermittent", "inguinal", "altered", "painless", "fullness", "passing", "robotic",
    "laparoscopic", "open", "elective", "emergency",
)
CLINICAL_WORDS = (
    "intestinal", "duodenal", "rectal", "umbilical", "paraumbilical", "periumbilical", "perianal",
    "perineal", "peritoneal", "retroperitoneal", "hepatic", "pancreatic", "peripancreatic",
    "gastric", "appendicular", "appendiceal", "diverticular", "mesenteric", "omental",
    "anastomotic", "peptic", "pyloric", "anorectal", "colorectal", "splenic", "sigmoid",
    "rectosigmoid", "cardia", "inguinal", "femoral", "incisional", "ventral", "abdominal",
    "pelvic", "renal", "biliary", "cystic", "colonic", "caecal", "cecal", "ileal", "jejunal",
    "ileocaecal", "ileocecal", "oesophageal", "esophageal", "hiatal", "hiatus", "pilonidal",
    "scrotal", "groin", "flank", "loin", "iliac", "fossa", "quadrant", "region", "diaphragm",
    "subphrenic", "subhepatic", "liver", "colon", "caecum", "cecum", "appendix", "gallbladder",
    "bile", "duct", "outlet", "wall", "bowel", "stump", "port", "site", "wound", "ileus",
    "gallstones", "gallstone", "stones", "calculi", "sludge", "pneumoperitoneum", "fatty",
    "hydatid", "amoebic", "pyogenic", "malignant", "benign", "metastatic", "neoplastic", "cancer",
    "carcinoma", "carcinomatosis", "free", "fluid", "gas", "air", "pus", "subacute", "koch",
    "kochs", "calculous", "acalculous", "achalasia", "intussusception", "ulcer", "ulcers",
    "perforation", "perforated", "growth", "hernia", "fistula", "fissure", "sinus", "prolapse",
    "obstruction", "obstructed", "strangulated", "incarcerated", "gangrenous", "volvulus",
    "abscess", "ascites", "mucocele", "pseudocyst", "necrosis", "necrotising", "necrotizing",
    "collection", "effusion", "leak", "bleed", "thickening", "thickened", "dilated", "distended",
    "enlarged", "impacted", "inflamed", "inflammatory", "infected", "tubercular", "tuberculous",
    "adhesions", "adhesive", "stricture", "narrowing", "nodule", "nodules", "varices", "injury",
    "trauma", "blunt", "infection", "sepsis", "septic", "shock", "dehiscence", "lump", "mass",
    "drain", "contrast", "conversion", "complications", "complication", "previous", "prior",
    "similar", "complaints", "complaint", "heaviness", "swelling", "pain", "sided", "side", "left",
    "right", "bilateral", "unilateral", "upper", "lower", "lateral", "medial", "anterior",
    "posterior", "proximal", "distal", "patient", "patients", "surgery", "surgeries", "operation",
    "procedure", "smoking", "smoker", "alcohol", "alcoholic", "tobacco", "chewing", "intake",
    "asthma", "copd", "thyroid", "stroke", "over", "under", "below", "above", "around", "within",
    "near", "acute", "chronic", "mild", "moderate", "severe", "sudden", "gradual", "intermittent",
    "continuous", "colicky", "dull", "recurrent", "grade", "stage", "type", "small", "large",
    "giant", "huge", "minimal", "gross", "multiple", "single", "early", "delayed", "partial",
    "complete", "focal", "diffuse", "residual", "healed", "healing", "surgical", "operative",
    "postoperative", "preoperative", "findings", "impression", "annular", "recurrence", "backache",
    "signet", "ring", "stomach", "oesophagus", "esophagus", "duodenum", "jejunum", "ileum", "anus",
    "omentum", "mesentery", "peritoneum", "spleen", "kidney", "ureter", "bladder", "uterus",
    "ovary", "ovarian", "breast", "lymph", "node", "nodes", "vein", "artery", "arterial", "venous",
    "portal", "hollow", "viscus", "gastrointestinal", "stromal", "hepatocellular", "periampullary",
    "ampullary", "choledochal", "subdiaphragmatic", "ureteric", "pulmonary", "ischemic",
    "ischaemic", "heart", "calcific", "ulcerative", "sliding", "simple", "complex", "solid",
    "mucinous", "serous", "twisted", "walled", "retained", "space", "occupying", "typhoid",
    "fibroid", "neoplasm", "metastases", "metastasis", "torsion", "gangrene", "infarct",
    "infarction", "disease", "section", "caesarean", "cesarean", "allergy", "drug", "urine",
    "stools", "stool", "dark", "clay", "coloured", "colored", "episodes", "episode", "transfusion",
    "accident", "traffic", "road", "lap", "chole", "cbd", "gerd", "hcc", "gist", "sol", "ibd",
    "ibs", "sbo", "ercp", "mrcp", "usg", "cect", "hpe", "fnac", "rta", "ihd", "cad", "htn", "ckd",
    "cva", "ptb", "uti", "dvt", "pud", "nafld", "nash", "lscs", "tah", "bso", "rif", "ruq", "luq",
)
HEAD_NOUNS = (
    "perforation", "growth", "cyst", "stone", "stones", "calculus", "lump", "mass", "tumour",
    "tumor", "carcinoma", "cancer", "lesion", "stricture", "obstruction", "abscess", "collection",
    "leak", "hernia", "fistula", "ulcer", "nodule", "nodules", "polyp", "neoplasm", "syndrome",
    "disease", "diverticulum", "infarct", "injury", "fluid", "effusion", "volvulus", "ileus",
    "ischemia", "ischaemia", "liver", "abdomen", "uterus",
)
LABEL_STOP = (
    "diagnosis", "prognosis", "occupation", "admission", "religion", "residence", "address",
)
IMAGING_WORDS = (
    "abdomen", "pelvis", "liver", "brain", "spine", "chest", "knee", "neck", "rectum", "pancreas",
    "contrast", "imaging", "scan", "study", "score", "protocol", "with", "without", "and", "for",
    "the", "grade",
)
GIVEN_NAMES = (
    "gopal", "vishal", "kunal", "sonal", "kamal", "komal", "nirmal", "vimal", "amal", "kushal",
    "ujjwal", "dhaval", "jamal", "bilal", "faisal", "iqbal", "sonia", "rajan", "kamala", "ramesh",
    "suresh", "mahesh", "rajesh", "ganesh", "lakshmi", "anjali", "pooja", "arjun", "prashant",
    "ramaiah", "nancy", "soma", "roma", "om", "manjunath", "nagaraj", "basavaraj", "venkatesh",
    "srinivas", "srinivasa", "murali", "mohan", "krishna", "shankar", "shiva", "siva",
    "subramanya", "raghavendra", "narayana", "narayan", "gowda", "reddy", "naidu", "nair", "menon",
    "pillai", "iyer", "rao", "ravi", "anil", "sunil", "kiran", "naveen", "praveen", "prakash",
    "anand", "arun", "vijay", "ajay", "kumar", "raju", "ramu", "babu", "anitha", "kavitha",
    "kavita", "sunitha", "geetha", "shanthi", "saroja", "sarojamma", "padma", "nirmala", "vimala",
    "shyamala", "mangala", "sushila", "parvathi", "bhavani", "yashoda", "savitri", "radha",
    "rekha", "usha", "asha", "uma", "devi", "meena", "priya", "divya", "kavya", "rahul", "rohit",
    "amit", "sanjay", "ashok", "manoj", "deepak", "raj", "ram", "sunita", "neha", "sharma",
    "verma", "gupta", "singh", "patel", "shah", "gurpreet", "harpreet", "mohammed", "mohammad",
    "abdul", "ahmed", "imran", "salim", "ali", "fatima", "ayesha", "joseph", "thomas", "mary",
)
EXT_STOP = (
    "uhid", "mrn", "ipd", "ip", "op", "reg", "hosp", "hospital", "patient", "pt", "age", "aged",
    "dob", "phone", "ph", "mob", "mobile", "pan", "aadhaar", "address", "contact", "tel", "call",
    "email", "date", "name", "door", "flat", "house", "plot", "sector", "january", "february",
    "march", "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
    "dec",
)


# ---------- helpers ----------


def word_key(w: str) -> str:
    return "".join(c for c in w if _is_alphabetic(c)).lower()


def inside_longer_number(text: str, start: int, end: int) -> bool:
    """True when the match is glued to neighbouring digits ("1234567890123",
    "2345-6789-0123-4567", "1.150000"), i.e. it is part of a longer number."""
    sep = ".-/\U00002013\U00002014"

    def digit(c: str) -> bool:
        return "0" <= c <= "9"

    prev = False
    if start >= 1:
        c = text[start - 1]
        prev = digit(c) or (c in sep and start >= 2 and digit(text[start - 2]))
    nxt = False
    if end < len(text):
        c = text[end]
        nxt = digit(c) or (c in sep and end + 1 < len(text) and digit(text[end + 1]))
    return prev or nxt


def standalone_number(t: _Text, m: _M, _f: list[Finding]) -> bool:
    return not inside_longer_number(t.s, m.start, m.end)


def always(_t: _Text, _m: _M, _f: list[Finding]) -> bool:
    return True


def token_key(tok: str) -> str:
    return tok.strip(_PUNCT).lower()


def starts_with_unit(rest: str, units: Iterable[str]) -> bool:
    """Starts with one of `units` (case-sensitive) followed by a non-alphanumeric char."""
    for u in units:
        if rest.startswith(u):
            r = rest[len(u):]
            if not (r and _is_alphanumeric(r[0])):
                return True
    return False


def phone_label_before(text: str, s: int) -> bool:
    """A phone label within the previous 3 tokens. "pH" (acidity) is never a label."""
    for tok in _last_tokens(text, s, 3):
        raw = tok.strip(_PUNCT)
        if raw != "pH" and raw.lower() in PHONE_LABELS:
            return True
    return False


_U64 = re.compile(r"\+?[0-9]+")


def _parse_u64(s: str) -> int | None:
    """Rust `str::parse::<u64>`: ASCII digits with an optional `+`, below 2**64."""
    if not _U64.fullmatch(s):
        return None
    v = int(s)
    return v if v < 2**64 else None


def looks_like_range(m: str) -> bool:
    """"65000-80000": two dash-separated round numbers in ascending order are a range."""
    parts = [_trim(p) for p in re.split("[-\U00002013\U00002014]", m)]
    if len(parts) != 2:
        return False
    x, y = _parse_u64(parts[0]), _parse_u64(parts[1])
    return x is not None and y is not None and x < y and x % 100 == 0 and y % 100 == 0


def phone_check(t: _Text, m: _M, _f: list[Finding]) -> bool:
    text, s, e = t.s, m.start, m.end
    if inside_longer_number(text, s, e):
        return False
    # A phone label nearby always wins over lab-value heuristics.
    if phone_label_before(text, s):
        return True
    before = [token_key(tok) for tok in _last_tokens(text, s, 3)]
    # A hyphen/en-dash range after a lab word is a lab range ("platelets 65000-80000").
    mt = text[s:e]
    ranged = any(c in mt for c in "-\U00002013\U00002014")
    if ranged and (looks_like_range(mt) or any(k in PHONE_VETO_WORDS for k in before)):
        return False
    # Followed by a real unit: a count, not a phone.
    rest = _trim_start(text[e:t.ahead(e, 12)])
    iu = (rest[:2].isascii() and _eq_ascii_ci(rest[:2], "iu")
          and not (len(rest) > 2 and _is_alphanumeric(rest[2])))
    return not (iu or starts_with_unit(rest, PHONE_UNITS))


def labelled_phone(t: _Text, m: _M, _f: list[Finding]) -> bool:
    """Any 10-digit mobile grouping ("98450 12 345"), only right after a phone label."""
    return not inside_longer_number(t.s, m.start, m.end) and phone_label_before(t.s, m.start)


def unit_follows(t: _Text, e: int) -> bool:
    """The text right after `e` is a measurement unit. Single-letter `g` counts only in lower
    case ("1 g"), so "45 G Block" stays an address."""
    rest = _trim_start(t.s[e:t.ahead(e, 8)])
    if rest.startswith("%"):
        return True
    return starts_with_unit(_ascii_lower(rest), UNITS_AFTER_NUMBER) or starts_with_unit(rest,
                                                                                         ("g",))


def no_unit_after(t: _Text, m: _M, _f: list[Finding]) -> bool:
    # "Flat 30-degree scope": the ID pattern swallows "-degree".
    if "-degree" in t.s[m.start:m.end].lower():
        return False
    return not unit_follows(t, m.end)


def numbered_label_ok(t: _Text, m: _M, _f: list[Finding]) -> bool:
    """"Case No. 2, A 60-year-old..." is a numbered case, not an address."""
    end = m.start
    while end > 0 and t.s[end - 1] in _WS:
        end -= 1
    i = end
    while i > 0 and _is_alphabetic(t.s[i - 1]):
        i -= 1
    prev = t.s[i:end].lower()
    return prev not in ("case", "table", "fig", "figure", "group", "arm", "study", "patient")


PLACE_BEFORE = re.compile(_py(r"[A-Z][a-z]{2,}(?:\s*[,\-\x{2013}\x{2014}]\s*|\s+)$"))


def pin_label_before(t: _Text, s: int) -> bool:
    lo = t.behind(s, 16)
    pre = t.s[lo:s].rstrip(WHITE_SPACE + ":-.#,(\U00002013").lower()
    for label in PIN_LABELS:
        if pre.endswith(label):
            rest = pre[: len(pre) - len(label)]
            if not (rest and _is_alphanumeric(rest[-1])):
                return True
    return False


def count_noun(w: str) -> bool:
    return w in COUNT_NOUNS or any(
        w.endswith(s) for s in ("ectomies", "otomies", "ostomies", "plasties", "scopies"))


def address_label_before(t: _Text, s: int) -> bool:
    """"Address" / "addr" within the previous 60 bytes."""
    lo = t.behind(s, 60)
    return any(_eq_ascii_ci(w, "address") or _eq_ascii_ci(w, "addr")
               for w in _alpha_words(t.s[lo:s]))


def _alpha_words(s: str) -> list[str]:
    """`s.split(|c| !c.is_alphabetic())`."""
    out, cur = [], []
    for c in s:
        if _is_alphabetic(c):
            cur.append(c)
        else:
            out.append("".join(cur))
            cur = []
    out.append("".join(cur))
    return out


def _take_alpha(s: str) -> str:
    i = 0
    while i < len(s) and _is_alphabetic(s[i]):
        i += 1
    return s[:i]


def pin_check(t: _Text, m: _M, addr: list[Finding]) -> bool:
    text, s, e = t.s, m.start, m.end
    if inside_longer_number(text, s, e):
        return False
    # A pin label always wins.
    if pin_label_before(t, s):
        return True
    # A lab/count word within the previous 3 tokens means this is a count.
    for tok in _last_tokens(text, s, 3):
        k = tok.strip(_PUNCT).lower()
        if k in LAB_WORDS or "\U000020b9" in tok:
            return False
    # Followed by a unit means this is a measurement (bounded lookahead).
    rest_full = text[e:t.ahead(e, 16)]
    rest = _trim_start(rest_full).lower()
    if any(rest.startswith(u) for u in UNIT_PREFIXES):
        return False
    # Strong address cues: an "Address" label or another address finding nearby (bytes).
    bs, be = t.b[s], t.b[e]
    starts = [t.b[f.start] for f in addr]
    lo = bisect.bisect_left(starts, max(0, bs - 200))
    addr_near = False
    for f in addr[lo:]:
        if t.b[f.start] > be + 60:
            break
        if t.b[f.end] + 60 >= bs:
            addr_near = True
            break
    if addr_near or address_label_before(t, s):
        return True
    # A count noun in the next two words outweighs a capitalised word before.
    if any(count_noun(_take_alpha(w)) for w in _split_ws(rest_full)[:2]):
        return False
    lo = t.behind(s, 40)
    return PLACE_BEFORE.search(t.sh[lo:s]) is not None


# ---------- names ----------


def followed_by_procedure(text: str, e: int) -> bool:
    """What follows the match at `e`: a possessive or a procedure word."""
    if text.startswith(("'s", "\U00002019"), e):
        return True
    i = e
    while i < len(text) and text[i] in _WS:
        i += 1
    j = i
    while j < len(text) and _is_alphabetic(text[j]):
        j += 1
    return text[i:j].lower() in PROC_WORDS


def name_check_titled(t: _Text, m: _M, _f: list[Finding]) -> bool:
    w = word_key(m.group("name") or "")
    if w in TITLE_COMMON:
        return False
    if w in EPONYMS:
        return not followed_by_procedure(t.s, m.end)
    return True


def name_check_context(t: _Text, m: _M, _f: list[Finding]) -> bool:
    w = word_key(m.group("name") or "")
    s = m.start
    head = t.s[s:s + 4]
    is_name_label = head.isascii() and _eq_ascii_ci(head, "name")
    if is_name_label and not name_label_ok(t.s, s, w):
        return False
    return name_candidate(w)


def name_label_ok(text: str, s: int, k: str) -> bool:
    """A "name" label starting at `s` names a person unless a non-person qualifier word
    directly precedes it on the same line, or its value `k` is a placeholder."""
    if k in NAME_PLACEHOLDERS:
        return False
    line = text[text.rfind("\n", 0, s) + 1:s]
    line = line.rstrip(WHITE_SPACE + ".")
    i = len(line)
    while i > 0 and _is_alphabetic(line[i - 1]):
        i -= 1
    return line[i:].lower() not in NON_PERSON_NAME_QUALIFIERS


def name_check_label(t: _Text, m: _M, _f: list[Finding]) -> bool:
    w = word_key(m.group("name") or "")
    s = m.group_start("label") or 0
    return name_label_ok(t.s, s, w) and name_candidate(w)


def exemption_head(k: str) -> bool:
    """A word that can close a clinical phrase ("Cyst", "Malformation", "Appendicitis")."""
    return (k in HEAD_NOUNS or k == "malformation"
            or (strong_medical(k) and not k.endswith("ation") and k not in LABEL_STOP))


def walk_stop(k: str) -> bool:
    return k in LABEL_STOP or k in ("impression", "findings", "provisional", "final",
                                    "diagnosed", "with", "without", "and", "for")


def diagnosis_phrase(run: list[tuple[str, bool]]) -> bool:
    for w, colon in run:
        if colon:
            return False
        k = word_key(w)
        if walk_stop(k):
            return False
        if exemption_head(k):
            return True
        if not clinical_word(k):
            return False
    return False


COMMA_AGE = re.compile(_py(r"^\s*,\s*([0-9]{2,3})\s*(?i:y|yrs?|years?)\b\.?"))
# The same pattern without its `^`, matched at a position (Python's `^` never matches at a
# `pos` other than 0; `match(text, pos)` anchors there).
_COMMA_AGE_AT = re.compile(COMMA_AGE.pattern[1:])


def _alpha_at(text: str, i: int) -> str:
    j = i
    while j < len(text) and _is_alphabetic(text[j]):
        j += 1
    return text[i:j]


def comma_age_follows(t: "_Text", e: int) -> bool:
    """"c/o Puttaswamy, 56 yrs": a comma then an age, unless a duration word follows."""
    m = _COMMA_AGE_AT.match(t.sh, e)
    if m is None:
        return False
    text, a = t.s, m.end()
    i = a
    while i < len(text) and not _is_alphanumeric(text[i]):
        i += 1
    nxt = _alpha_at(text, i).lower()
    if nxt == "of":
        j = i + 2
        while j < len(text) and text[j] in _WS:
            j += 1
        return _alpha_at(text, j).lower() == "age"
    if nxt == "history":
        return "," in text[a:i]
    return nxt not in ("ago", "back", "before", "since", "duration")


def not_name(k: str) -> bool:
    return k in NOT_NAMES or k in NOT_NAMES_2 or k in CLINICAL_WORDS


def name_candidate(k: str) -> bool:
    """An allowlisted given name (always), or a word of 3+ bytes that is not clinical."""
    return k in GIVEN_NAMES or (len(k.encode("utf-8", "surrogatepass")) >= 3
                                and not clinical_word(k))


def strong_medical(k: str) -> bool:
    return any(k.endswith(s) for s in STRONG_SUFFIXES)


def imaging_word(k: str) -> bool:
    return k in IMAGING_WORDS or k.startswith("angio") or k.endswith("gram")


def clinical_word(k: str) -> bool:
    return strong_medical(k) or k in EPONYMS or not_name(k) or imaging_word(k)


def name_check_caps_title(_t: _Text, m: _M, _f: list[Finding]) -> bool:
    w = word_key(m.group("name") or "")
    return w not in TITLE_COMMON and name_candidate(w)


AGE_SEX_AFTER = re.compile(_py(
    r"^[,\s]*(?:aged\s+[0-9]+|(?P<n1>[0-9]{1,3})\s*[MFmf]\b|(?P<n2>[0-9]{1,3})\s*(?:yo\b|y/o\b|"
    r"(?:y|yrs?|years?)\s*(?:old\b|/\s*[MFmf]\b|[MFmf]\b)))"
))


def age_follows(t: _Text, e: int) -> bool:
    """An age/sex marker right after the name; small numbers before M/F are months."""
    hi = t.ahead(e, 28)
    m = AGE_SEX_AFTER.match(t.sh[e:hi])
    if m is None:
        return False
    n = m.group("n1") or m.group("n2")
    return (int(n) if n is not None else 99) >= 10


def _skip_blanks(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def next_cap_word(text: str, pos: int) -> tuple[str, int] | None:
    """The next word after `pos` if it is a capitalised word of 3+ ASCII letters on the same
    line (at least one space or tab first), with the index where it ends."""
    i = _skip_blanks(text, pos)
    if i == pos:
        return None
    j = i
    while j < len(text) and text[j].isascii() and text[j].isalpha():
        j += 1
    w = text[i:j]
    return (w, j) if len(w) >= 3 and "A" <= w[0] <= "Z" else None


def cap_run(text: str, pos: int, n: int) -> list[tuple[str, bool]]:
    """Up to `n` capitalised words directly following `pos`, separated only by spaces/tabs,
    each with whether a `:` follows it."""
    out: list[tuple[str, bool]] = []
    while len(out) < n:
        got = next_cap_word(text, pos)
        if got is None:
            break
        w, pos = got
        k = _skip_blanks(text, pos)
        out.append((w, text.startswith(":", k)))
    return out


def relation_check(t: _Text, m: _M, _f: list[Finding]) -> bool:
    rel_raw = m.group("rel")
    rel = rel_raw.lower() if rel_raw is not None else "s"
    strict = rel in ("s", "d")
    k1 = word_key(m.group("w1") or "")
    if not name_candidate(k1):
        return False
    e = m.end
    title = m.group("title")
    titled = title is not None and bool(_trim(title))
    given = k1 in GIVEN_NAMES
    run = cap_run(t.s, e, 3)
    if titled or given:
        return True
    if strict:
        return not diagnosis_phrase(run)
    return (age_follows(t, e)
            or (rel in ("c", "w") and comma_age_follows(t, e))
            or (bool(run) and name_candidate(word_key(run[0][0])) and not diagnosis_phrase(run)))


def extend_name(text: str, end: int, extra: int) -> int:
    """Extend a name over up to `extra` following capitalised words that are not clinical
    words or the start of another identifier ("Kunal" -> "Kunal Shah")."""
    for _ in range(extra):
        got = next_cap_word(text, end)
        if got is None:
            break
        w, after = got
        k = word_key(w)
        if clinical_word(k) or k in EXT_STOP:
            break
        end = after
    return end


# ---------- address ----------


def digit_nearby(t: _Text, s: int, e: int) -> bool:
    lo, hi = t.behind(s, 20), t.ahead(e, 20)
    return any("0" <= c <= "9" for c in t.s[lo:s]) or any("0" <= c <= "9" for c in t.s[e:hi])


def locality_check(t: _Text, m: _M, _f: list[Finding]) -> bool:
    return digit_nearby(t, m.start, m.end)


# ---------- rules ----------

Check = Callable[[_Text, _M, list[Finding]], bool]


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: str  # Incisive's text, before `pat`
    check: Check
    deferred: bool = False  # runs after the others and may look at their Address findings
    extra_words: int = 0  # extend the finding over up to this many following name words


# deid.rs `RULES`, in order; each pattern is Incisive's text.
RULES: tuple[Rule, ...] = (
    # Aadhaar: 12 digits, first 2-9, 4-4-4 with space/hyphen/dot or contiguous.
    Rule("aadhaar", r"\b[2-9]\d{3}$A\d{4}$A\d{4}\b", standalone_number),
    # PAN: AAAAA9999A.
    Rule("pan", r"\b[A-Z]{5}\d{4}[A-Z]\b|(?i:\bPAN\b)(?i:\s*(?:no|number)\b)?\.?[\s:#\-]*[A-Za-z]{5}\d{4}[A-Za-z]\b", always),
    # Indian mobile: optional +91 / 91 / 0, 10 digits starting 6-9.
    Rule("phone", r"(?:\+91$H?|\b91$H|\b0)?(?:[6-9]\d{4}$H?\d{5}|[6-9]\d{2}$H\d{3}$H\d{4}|[6-9]\d{3}$H\d{3}$H\d{3}|[6-9]\d$H\d{3}$H\d{5})\b", phone_check),
    # Any other grouping of a 10-digit mobile, only after a phone label.
    Rule("phone", r"(?:\+91$H?|\b0$H?|\b)[6-9](?:$H?\d){9}\b", labelled_phone),
    # Landline with STD code (0-prefixed, or +91 prefixed).
    Rule("phone", r"(?:\+91$H?)?\(?\b0\d{2,4}\)?$H?\d{3,4}$H?\d{3,4}\b", standalone_number),
    Rule("phone", r"\+91$H?\(?\d{2,5}\)?$H?\d{3,4}$H?\d{3,4}\b", standalone_number),
    Rule("email", r"[A-Za-z0-9._%+\-]+[@\x{FF20}][A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", always),
    # Dates.
    Rule("calendar_date", r"\b$DAY$DS(?:0?[1-9]|1[0-2])$DS(?:\d{4}|\d{2})\b", standalone_number),
    Rule("calendar_date", r"\b(?:19|20)\d{2}$DS(?:0?[1-9]|1[0-2])$DS$DAY\b", standalone_number),
    Rule("calendar_date", r"(?i)\b$DAY(?:st|nd|rd|th)?$H+(?:of\s+)?(?:$MON)\b\.?(?:,?$H+\d{4}\b|[\-\x{2013}\x{2014}']\d{2}\b)?", always),
    Rule("calendar_date", r"(?i)\b$DAY(?:st|nd|rd|th)$H+(?:of\s+)?may\b|\b$DAY$H+may,?$H+\d{4}\b", always),
    Rule("calendar_date", r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+$DAY(?:st|nd|rd|th)?\b(?:,?\s+\d{4}\b)?", always),
    # Hospital IDs: label then an ID containing at least one digit.
    Rule("hospital_id", r"(?i)\b(?:(?:UHID|MRN|IPD|Patient\s*ID)(?:\s*(?:no|number)\b)?|(?:MRD?|IP|OP|Reg(?:istration)?\.?|Hosp(?:ital)?\.?)\s*(?:no|number)\b)\.?\s*[:#.\-\x{2013}]*\s*#?\s*(?:[A-Za-z]{1,6}[\-/ ]?)?\d[A-Za-z0-9/\-]*", always),
    Rule("hospital_id", r"\bIP\s+\d+(?:/\d+)?\b", no_unit_after),
    # Names: title + capitalised word (eponyms only exempt in procedure context).
    Rule("person_name", r"\b(?:Mr|Mrs|Ms|Miss|Shri|Smt|Kumari|Master|Baby\s+of)\b\.?\s*(?P<name>[A-Z][A-Za-z]+)", name_check_titled, extra_words=2),
    # All-caps titles need an all-caps name ("MR" alone is MR imaging).
    Rule("person_name", r"\b(?:MR|MRS|SHRI|SMT|KUMARI)\b\.?\s*(?P<name>[A-Z]{3,})\b", name_check_caps_title, extra_words=2),
    # Names: patient-context word + capitalised word.
    Rule("person_name", r"\b(?:[Pp]atient|[Pp]t|[Nn]ame|named|called)\b\.?(?:\s*[:,]\s*|\s+[\-\x{2013}\x{2014}]\s*|\s*[\x{2013}\x{2014}]\s+|\s+)(?:(?:is|was)\s+)?(?P<name>[A-Z][a-z]+)\b", name_check_context, extra_words=2),
    # Names: case-insensitive name label ("NAME: RAMAKKA", "Pt. Name - Ramakka").
    Rule("person_name", r"(?i:\b(?:(?:patient|pt)\.?[ \t]*)?(?P<label>name))[ \t]*[:\-\x{2013}\x{2014}][ \t]*(?P<name>[A-Z][a-z]+|[A-Z]{2,})\b", name_check_label, extra_words=2),
    # Relations: s/o, w/o, d/o, c/o + name.
    Rule("person_name", r"(?i:\b(?:(?P<rel>[sdwch])/o\b|(?:son|daughter|wife|husband)[ \t]+of\b))[ \t]*[:.\-\x{2013}\x{2014}]?[ \t]*(?P<title>(?:(?i:dr|late|sri|shri|smt|mr|mrs|ms)\b[.:]?[ \t]*)*)(?P<w1>[A-Z][A-Za-z]+)\b", relation_check, extra_words=2),
    # Addresses.
    Rule("address", r"(?i)\b(?:door|flat|house|plot)\s*(?:no\.?|number|#)\s*[A-Za-z]?\d[A-Za-z0-9/\-]*", no_unit_after),
    Rule("address", r"\b(?:Door|Flat|House|Plot)\s+\d[A-Za-z0-9/\-]*", no_unit_after),
    Rule("address", r"\bNo\.\s*\d+[A-Za-z]?,\s*[A-Z]", numbered_label_ok),
    Rule("address", r"\bSector\s+\d{1,3}\b", always),
    Rule("address", r"(?i)\B#\s?\d{1,5}[A-Za-z]?\b[^\n.;]{0,40}?\b(?:cross,|cross\s+(?:road|rd)\b|main\s+(?:road|rd)\b|road\b|rd\b|layout\b|nagar\b|street\b|colony\b|sector\b)", always),
    Rule("address", r"(?i)\b\d{1,3}(?:st|nd|rd|th)\s+cross\b(?:\s*,|\s+(?:road|rd|main|street)\b|\.?\s*$)|\b\d{1,3}(?:st|nd|rd|th)?\s+(?:cross|main)\s+(?:road|rd)\b", always),
    Rule("address", r"\b\d{1,4}[A-Za-z]?,?\s+(?:[A-Z][A-Za-z.]*\s+){1,3}(?:Road|Rd|Street|Marg|Lane|Avenue)\b", always),
    Rule("address", r"\b[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)\s+(?:Block|Phase)\b", always),
    Rule("address", r"\b[A-Za-z]*[nN]agar\b", locality_check),
    Rule("address", r"\b[A-Z][A-Za-z]+\s+Layout\b", locality_check),
    Rule("address", r"\b[1-8]\d{5}\b|\b[1-8]\d{2}$SP\d{3}\b", pin_check, deferred=True),
)


@cache
def _compiled(i: int) -> re.Pattern:
    return re.compile(pat(RULES[i].pattern))


def _run_rule(i: int, t: _Text, ctx: list[Finding], out: list[Finding]) -> None:
    """When a candidate is rejected the search restarts one character after its start, so a
    rejected "Patient Name" cannot swallow "Name: Ramesh" (deid.rs `run_rule`)."""
    r = RULES[i]
    rx = _compiled(i)
    pos = 0
    n = len(t.s)
    while pos <= n:
        mm = rx.search(t.sh, pos)
        if mm is None:
            break
        m = _M(t, mm)
        start, end = mm.span()
        if r.check(t, m, ctx):
            if r.extra_words > 0:
                end = extend_name(t.s, end, r.extra_words)
            out.append(Finding(r.kind, start, end))
            pos = max(end, start + 1)
        else:
            pos = start + 1


# ---------- character allowlist (ruling R-F3) ----------

# Latin and Greek script ranges (Unicode 16.0 Scripts.txt, merged; the tables Rust's regex
# crate 0.8 uses for \p{Script=Latin} and \p{Script=Greek}).
_LATIN_GREEK = (
    (0x0041, 0x005A),
    (0x0061, 0x007A),
    (0x00AA, 0x00AA),
    (0x00BA, 0x00BA),
    (0x00C0, 0x00D6),
    (0x00D8, 0x00F6),
    (0x00F8, 0x02B8),
    (0x02E0, 0x02E4),
    (0x0370, 0x0373),
    (0x0375, 0x0377),
    (0x037A, 0x037D),
    (0x037F, 0x037F),
    (0x0384, 0x0384),
    (0x0386, 0x0386),
    (0x0388, 0x038A),
    (0x038C, 0x038C),
    (0x038E, 0x03A1),
    (0x03A3, 0x03E1),
    (0x03F0, 0x03FF),
    (0x1D00, 0x1D2A),
    (0x1D2C, 0x1D77),
    (0x1D79, 0x1DBF),
    (0x1E00, 0x1F15),
    (0x1F18, 0x1F1D),
    (0x1F20, 0x1F45),
    (0x1F48, 0x1F4D),
    (0x1F50, 0x1F57),
    (0x1F59, 0x1F59),
    (0x1F5B, 0x1F5B),
    (0x1F5D, 0x1F5D),
    (0x1F5F, 0x1F7D),
    (0x1F80, 0x1FB4),
    (0x1FB6, 0x1FC4),
    (0x1FC6, 0x1FD3),
    (0x1FD6, 0x1FDB),
    (0x1FDD, 0x1FEF),
    (0x1FF2, 0x1FF4),
    (0x1FF6, 0x1FFE),
    (0x2071, 0x2071),
    (0x207F, 0x207F),
    (0x2090, 0x209C),
    (0x2126, 0x2126),
    (0x212A, 0x212B),
    (0x2132, 0x2132),
    (0x214E, 0x214E),
    (0x2160, 0x2188),
    (0x2C60, 0x2C7F),
    (0xA722, 0xA787),
    (0xA78B, 0xA7CD),
    (0xA7D0, 0xA7D1),
    (0xA7D3, 0xA7D3),
    (0xA7D5, 0xA7DC),
    (0xA7F2, 0xA7FF),
    (0xAB30, 0xAB5A),
    (0xAB5C, 0xAB69),
    (0xFB00, 0xFB06),
    (0xFF21, 0xFF3A),
    (0xFF41, 0xFF5A),
    (0x10140, 0x1018E),
    (0x101A0, 0x101A0),
    (0x10780, 0x10785),
    (0x10787, 0x107B0),
    (0x107B2, 0x107BA),
    (0x1D200, 0x1D245),
    (0x1DF00, 0x1DF1E),
    (0x1DF25, 0x1DF2A),
)
_LG_STARTS = tuple(a for a, _ in _LATIN_GREEK)


def _latin_or_greek(o: int) -> bool:
    i = bisect.bisect_right(_LG_STARTS, o) - 1
    return i >= 0 and o <= _LATIN_GREEK[i][1]


def allowed_letter(c: str) -> bool:
    """A Latin or Greek letter, outside the Halfwidth/Fullwidth Forms and Mathematical
    Alphanumeric Symbols blocks."""
    o = ord(c)
    return (o >= 0x80 and not 0xFF00 <= o <= 0xFFEF and not 0x1D400 <= o <= 0x1D7FF
            and unicodedata.category(c)[0] == "L" and _latin_or_greek(o))


def is_mark(c: str) -> bool:
    return ord(c) >= 0x80 and unicodedata.category(c)[0] == "M"


_SYMBOLS = frozenset(
    "\xb0\xb1\xd7\xf7\xb5\U00002265\U00002264\U00002260\U00002248\U0000223c\U00002192"
    "\U00002190\U00002191\U00002193\U00002019\U00002018\U0000201c\U0000201d\U00002022"
    "\U00002026\xae\U00002122\xa9\xa7\U000020ac\xa3\U000020b9\U00002030"
    "\U0000207a\U0000207b\U0000208a\U0000208b"
)
_CONTEXTUAL = frozenset(
    "\U00002013\U00002014\U00002010\U00002011\U00002212\xb7\xa0\U0000202f")


def allowed_symbol(c: str) -> bool:
    return c in _SYMBOLS


def contextual(c: str) -> bool:
    """Allowed only when neither neighbour is an ASCII digit."""
    return c in _CONTEXTUAL


def script_digit(c: str) -> bool:
    o = ord(c)
    return o in (0xB2, 0xB3, 0xB9, 0x2070) or 0x2074 <= o <= 0x2079 or 0x2080 <= o <= 0x2089


def vulgar_fraction(c: str) -> bool:
    return 0xBC <= ord(c) <= 0xBE


def unit_mark(text: str, start: int, end: int) -> bool:
    """A short run of superscript/subscript digits or one fraction that reads as a unit or an
    exponent ("m²", "×10⁹/L", "SpO₂", "Ca²⁺", "1½"), never when another digit follows."""
    run = text[start:end]
    if len(run) > 2 or (end < len(text) and _is_numeric(text[end])):
        return False
    before = text[:start]
    if all(script_digit(c) for c in run):
        prev = before[-1] if before else None
        exponent = before.endswith("10") and not (len(before) > 2 and _is_numeric(before[-3]))
        return exponent or (prev is not None and (_is_alphabetic(prev)
                                                  or prev in ")\U0000207a\U0000207b"))
    return (len(run) == 1 and vulgar_fraction(run[0])
            and not (before and _is_numeric(before[-1]) and not "0" <= before[-1] <= "9"))


def allowed_letter_or_ascii_letter(c: str) -> bool:
    return (c.isascii() and c.isalpha()) or allowed_letter(c)


def unusual_characters(text: str) -> list[Finding]:
    """Every maximal run of characters outside the allowlist (rejected, never normalised):
    printable ASCII, tab, newline and CR; Latin and Greek letters; up to two combining marks
    directly after such a letter; the clinical symbols, and script digits and fractions that
    read as a unit mark; en/em dash, U+2010, U+2011, minus, middle dot, NBSP and narrow NBSP
    only when neither neighbour is an ASCII digit."""
    n = len(text)

    def digit_at(j: int) -> bool:
        return 0 <= j < n and "0" <= text[j] <= "9"

    allowed = [False] * n
    unit_candidate = [False] * n
    for i, c in enumerate(text):
        if c.isascii():
            allowed[i] = " " <= c <= "~" or c in "\t\n\r"
        elif allowed_letter(c) or allowed_symbol(c):
            allowed[i] = True
        elif contextual(c):
            allowed[i] = not digit_at(i - 1) and not digit_at(i + 1)
        elif is_mark(c):
            j, marks = i, 0
            while j > 0 and is_mark(text[j - 1]):
                j -= 1
                marks += 1
            allowed[i] = marks < 2 and j > 0 and allowed_letter_or_ascii_letter(text[j - 1])
        else:
            unit_candidate[i] = script_digit(c) or vulgar_fraction(c)
    i = 0
    while i < n:
        if not unit_candidate[i]:
            i += 1
            continue
        j = i
        while j < n and unit_candidate[j]:
            j += 1
        if unit_mark(text, i, j):
            for k in range(i, j):
                allowed[k] = True
        i = j
    out: list[Finding] = []
    i = 0
    while i < n:
        if allowed[i]:
            i += 1
            continue
        j = i
        while j < n and not allowed[j]:
            j += 1
        out.append(Finding("unusual_characters", i, j))
        i = j
    return out


def without_marks(text: str) -> tuple[str, list[int]]:
    """`text` without combining marks, and for each character of the result (plus its end)
    the matching index in `text`. Detection only: the question is never rewritten."""
    out: list[str] = []
    idx: list[int] = []
    for i, c in enumerate(text):
        if is_mark(c):
            continue
        out.append(c)
        idx.append(i)
    idx.append(len(text))
    return "".join(out), idx


def rule_findings(text: str) -> list[Finding]:
    """The identifier rules (not the character allowlist) over `text`."""
    t = _Text(text)
    found: list[Finding] = []
    for i, r in enumerate(RULES):
        if not r.deferred:
            _run_rule(i, t, [], found)
    addr = sorted((f for f in found if f.kind == "address"), key=lambda f: f.start)
    for i, r in enumerate(RULES):
        if r.deferred:
            _run_rule(i, t, addr, found)
    return found


def scan(text: str) -> list[Finding]:
    """Patient identifiers in `text`, sorted by start; overlapping findings are merged into one
    (kind of the earliest, longest first)."""
    found = unusual_characters(text)
    found.extend(rule_findings(text))
    if any(is_mark(c) for c in text):
        bare, idx = without_marks(text)
        found.extend(Finding(f.kind, idx[f.start], idx[f.end]) for f in rule_findings(bare))
    found.sort(key=lambda f: (f.start, -f.end))
    merged: list[Finding] = []
    for f in found:
        if merged and f.start < merged[-1].end:
            last = merged[-1]
            merged[-1] = Finding(last.kind, last.start, max(last.end, f.end))
        else:
            merged.append(f)
    return merged
