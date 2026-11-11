"""Compare the writer's texts in `writer/prompts.py` with Incisive's Rust source, read from disk.

Usage: python scripts/check_incisive_texts.py <incisive-root>

Reads the string literals by pinned line range from Incisive's
`crates/incisive-app/src/agent/writer_v3.rs` (commit 12630f7), decodes Rust's escapes (`\\"`,
`\\n`, `\\\\`, `\\u{..}`, and a line-end `\\` dropping the newline and the next line's leading
whitespace), and compares each with the port. Prints only `equal`, or one line per difference,
`differs <name> at character N` (N counts from 0). Never prints prompt text. Exit 0 when equal.

Not in CI: there is no Incisive checkout there.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pubmedgemini.researcher.loop import SearchEntry
from pubmedgemini.researcher.studylist import MAX_CONTRADICTIONS, Contradiction
from pubmedgemini.writer import prompts as p

WRITER = "crates/incisive-app/src/agent/writer_v3.rs"


class Moved(Exception):
    """A pinned line range no longer holds what it held at commit 12630f7."""


# ---------- reading Rust ----------


def decode(raw: str) -> str:
    """The value of a Rust (non-raw) string literal's body."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt == "\n":  # line continuation: newline and following whitespace dropped
            i += 2
            while i < len(raw) and raw[i] in " \t\n\r":
                i += 1
            continue
        simple = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "0": "\0", "'": "'", '"': '"'}
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt == "x":
            out.append(chr(int(raw[i + 2:i + 4], 16)))
            i += 4
        elif nxt == "u":
            end = raw.index("}", i)
            out.append(chr(int(raw[i + 3:end].replace("_", ""), 16)))
            i = end + 1
        else:
            raise Moved(f"unknown escape at {i}")
    return "".join(out)


def literals(code: str) -> list[str]:
    """Every string literal in `code`, decoded, in order; comments and char literals skipped."""
    out: list[str] = []
    i = 0
    while i < len(code):
        if code.startswith("//", i):
            i = code.find("\n", i)
            if i == -1:
                break
            continue
        ch = code[i]
        if ch == "'":
            m = re.match(r"'(\\.|\\u\{[0-9a-fA-F_]+\}|[^\\'])'", code[i:])
            i += m.end() if m else 1  # otherwise a lifetime
            continue
        if ch == '"':
            j = i + 1
            while code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            out.append(decode(code[i + 1:j]))
            i = j + 1
            continue
        i += 1
    return out


class Source:
    def __init__(self, path: Path):
        self.lines = path.read_text(encoding="utf-8").split("\n")

    def code(self, first: int, last: int, must_start: str) -> str:
        """Lines `first`..`last` (1-based, inclusive); the first must start with `must_start`
        once leading spaces are dropped."""
        if not self.lines[first - 1].lstrip().startswith(must_start):
            raise Moved(f"line {first}")
        return "\n".join(self.lines[first - 1:last])

    def one(self, first: int, last: int, must_start: str) -> str:
        found = literals(self.code(first, last, must_start))
        if len(found) != 1:
            raise Moved(f"lines {first}-{last}: {len(found)} literals")
        return found[0]

    def number(self, line: int, must_start: str) -> int:
        m = re.search(r"[:=]\s*([0-9_]+)\s*[,;]", self.code(line, line, must_start))
        if not m:
            raise Moved(f"line {line}")
        return int(m.group(1).replace("_", ""))


# ---------- comparing ----------


def first_difference(a: str, b: str) -> int | None:
    for k, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return k
    return None if len(a) == len(b) else min(len(a), len(b))


def render(template: str, named: dict[str, str], positional: list[str]) -> str:
    """Rust `format!` for `{}` and `{NAME}` (no `{{`, `}}` or specs in these templates)."""
    if "{{" in template or "}}" in template:
        raise Moved("format escapes")
    args = iter(positional)

    def sub(m: re.Match) -> str:
        return named[m.group(1)] if m.group(1) else next(args)

    out = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)?\}", sub, template)
    if next(args, None) is not None:
        raise Moved("unused format argument")
    return out


def fragments_match(template: str, texts: list[str]) -> int | None:
    """None when `template`'s fixed pieces appear, in order and contiguous around each
    placeholder, in one of `texts`; otherwise the character in `template` where the longest
    match stops."""
    holes = list(re.finditer(r"\{[A-Za-z_]*\}", template))
    starts = [0] + [h.end() for h in holes]  # where each fixed piece starts in `template`
    pieces = re.split(r"\{[A-Za-z_]*\}", template)
    best = 0
    for text in texts:
        pattern = ""
        for k, piece in enumerate(pieces):
            pattern += ("(?:.*?)" if k else "") + re.escape(piece)
            if not re.search(pattern, text, re.DOTALL):
                best = max(best, starts[k])
                break
        else:
            return None
    return best


def check(incisive_root: Path) -> list[str]:
    src = Source(incisive_root / WRITER)
    diffs: list[str] = []

    def same(name: str, rust: str, port: str) -> None:
        k = first_difference(rust, port)
        if k is not None:
            diffs.append(f"differs {name} at character {k}")

    # Literals (w.rs:105-157, 1733).
    same("SYSTEM", src.one(105, 153, "pub const SYSTEM: &str ="), p.SYSTEM)
    same("STUDIES_HEADING", src.one(156, 156, "pub const STUDIES_HEADING: &str ="),
         p.STUDIES_HEADING)
    same("SUBMIT", src.one(157, 157, "const SUBMIT: &str ="), p.SUBMIT)
    section_head = src.one(1733, 1733, "pub const SECTION_HEAD: &str =")
    same("SECTION_HEAD", section_head, p.SECTION_HEAD)

    # Caps (w.rs:74-87), runs/mod.rs:162, researcher.rs:117.
    rust_caps = {
        "title": src.number(75, "title:"), "journal": src.number(76, "journal:"),
        "pub_types": src.number(77, "pub_types:"), "doi": src.number(78, "doi:"),
        "abstract": src.number(79, "abstract_text:"), "why": src.number(80, "why:"),
        "note": src.number(81, "note:"), "query": src.number(82, "query:"),
    }
    for k, v in rust_caps.items():
        same(f"STUDY_CAPS.{k}", str(v), str(getattr(p.STUDY_CAPS, k)))
    same("DESIGN_BYTES", str(src.number(85, "const DESIGN_BYTES")), str(p.DESIGN_BYTES))
    same("WRITER_SEARCHES", str(src.number(87, "pub const WRITER_SEARCHES")),
         str(p.WRITER_SEARCHES))
    mod = Source(incisive_root / "crates/incisive-app/src/runs/mod.rs")
    same("WRITER_MAX_TOKENS", str(mod.number(162, "const WRITER_MAX_TOKENS")),
         str(p.WRITER_MAX_TOKENS))
    res = Source(incisive_root / "crates/incisive-app/src/agent/researcher.rs")
    same("MAX_CONTRADICTIONS", str(res.number(117, "pub const MAX_CONTRADICTIONS")),
         str(MAX_CONTRADICTIONS))
    ans = Source(incisive_root / "crates/incisive-app/src/agent/answer_v3.rs")
    certainty = literals(ans.code(47, 47, "pub const ALL: [&'static str; 4]"))
    schema_enum = p.writer_schema()["properties"]["bottom_line"]["properties"]["certainty"]
    same("Certainty::ALL", "\n".join(certainty), "\n".join(schema_enum["enum"]))

    # Sections (w.rs:1756-1759 name, 1776-1779 fields, 1786-1797 covers, 1826-1837 instruction).
    order = ["Trials", "Outcomes", "Context", "Summary"]
    names = [src.one(1756 + i, 1756 + i, f"Section::{v} =>") for i, v in enumerate(order)]
    fields = [literals(src.code(1776 + i, 1776 + i, f"Section::{v} =>"))
              for i, v in enumerate(order)]
    covers = [src.one(1786 + 3 * i, 1788 + 3 * i, f"Section::{v} =>")
              for i, v in enumerate(order)]
    bullet = src.one(1826, 1826, ".map(|s| format!(")
    template = src.one(1828, 1833, "format!(")
    if [s.name for s in p.Section.ALL] != names:
        diffs.append("differs Section.ALL at character 0")
    for i, s in enumerate(p.Section.ALL):
        same(f"Section.{order[i].upper()}.name", names[i], s.name)
        same(f"Section.{order[i].upper()}.fields", "\n".join(fields[i]), "\n".join(s.fields))
        same(f"Section.{order[i].upper()}.covers", covers[i], s.covers)
        others = "\n".join(render(bullet, {}, [covers[j]]) for j in range(4) if j != i)
        rust_instruction = render(template, {"SECTION_HEAD": section_head},
                                  [names[i].upper(), ", ".join(fields[i]), others])
        same(f"Section.{order[i].upper()}.instruction", rust_instruction, s.instruction())

    # The user message's fixed text (w.rs:733-871: capped .. notes_tail). Each literal's
    # pieces must appear, in order around its placeholders, in a message the port builds.
    users = sample_messages()
    for line, lit in user_literals(src):
        k = fragments_match(lit, users)
        if k is not None:
            diffs.append(f"differs user-message literal at w.rs:{line} at character {k}")
    return diffs


def user_literals(src: Source) -> list[tuple[int, str]]:
    """The string literals of w.rs:733-871, each with its line."""
    src.code(733, 733, "fn capped(")
    src.code(871, 871, "}")
    if "fn notes_tail(" not in src.code(733, 871, "fn capped("):
        raise Moved("notes_tail")
    out = []
    for n in range(733, 872):
        for lit in literals(src.lines[n - 1]):
            out.append((n, lit))
    # A literal split over lines would be missed above: none in this range.
    if len(literals(src.code(733, 871, "fn capped("))) != len(out):
        raise Moved("a multi-line literal in the user message code")
    return out


def sample_messages() -> list[str]:
    """Messages covering every branch of the user-message code."""
    full = p.StudyInput(pmid="11111111", title="T", journal="J", year=2025,
                        pub_types=("A", "B"), doi="10.1/x", retracted=False,
                        abstract="x" * 6000, design="RCT", why="w")
    bare = p.StudyInput(pmid="22222222", title="T", journal=None, year=None, pub_types=(),
                        doi=None, retracted=True, abstract=None, design="other", why="w")
    short = p.StudyInput(pmid="33333333", title="T", journal="J", year=2020, pub_types=("A",),
                         doi="d", retracted=False, abstract="a b", design="SR_MA", why="w")
    with_all = p.writer_user(
        "q", [full, bare, short],
        [Contradiction(pmid="11111111", note="n")],
        [SearchEntry(query="q", source="PubMed", hits=3)],
    )
    empty = p.writer_user("q", [short], [], [])
    return [with_all, empty]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_incisive_texts.py <incisive-root>")
        return 2
    try:
        diffs = check(Path(argv[1]))
    except (Moved, OSError, IndexError, ValueError, StopIteration) as e:
        # The reason names a line range or a count, never text from the file.
        print(f"differs source layout ({type(e).__name__}: {e if isinstance(e, Moved) else ''})")
        return 1
    if diffs:
        print("\n".join(diffs))
        return 1
    print("equal")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
