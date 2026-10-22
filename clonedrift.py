#!/usr/bin/env python
"""Name every script on a share that exists in more than one version, and say what the versions disagree about.

A scheduled job pulls a parcel roll off a vendor host, empties the production
feature class and appends what it got. Somebody fixes a bug in it. Nothing
changes at 2am, because the scheduler runs a different copy. On the share this
was built against, one script name occurs 19 times in 5 versions, and another
occurs 15 times in 4 versions where two siblings differ by one edit and one
stray space: one runs os.remove and rmtree on the source folder, and the other
has those lines commented out. Run the wrong copy and the extract is gone.

jscpd and copydetect already find duplicated code, they are maintained, and
both read Python 2 without complaint. This is not a new clone detector and does
not try to be one. Those tools answer "how much of this tree is duplicated".
This one answers "which of these 15 copies has rmtree live, and which host does
each one name", by grouping whole files that share a name, collapsing copies
that differ only in comments or whitespace, and ranking each surviving
disagreement by whether it touches a destructive call or a path literal. A
byte-for-byte deduplicator sees none of it, because the copies are not
identical. litswap rewrites a literal you already name and stalehost finds a
host you already name; this is the tool that tells you which name to give them.

    python clonedrift.py /path/to/share
    python clonedrift.py /path/to/share --show-diff
    python clonedrift.py /path/to/share --min-class cosmetic --fail-on path
    python clonedrift.py /path/to/share --near 0.8
    python clonedrift.py /path/to/share --out drift.json --apply
    python clonedrift.py --parity PARITY.md --map "C:\\work=/mnt/work"
    python clonedrift.py --self-test

--parity is the same question asked of a document. A parity table says two
copies of a file are identical, usually because the sizes matched. This reads
the table, finds each row that says so, opens the copies and compares the
bytes. A path it cannot honestly check is sorted into a class instead of being
called missing: foreign (a Windows path on a Linux host, or the reverse),
ambiguous (a placeholder, a relative path, a drive-relative path) or corrupt (a
control byte, which is what a pasted "\\a" leaves in a path).

Exit codes: 0 clean, 1 drift at or above --fail-on (or, with --parity, a claim
the files contradict), 2 nothing could be scanned, 64 usage error.
"""


import argparse
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tokenize

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Extensions read by default. This ranks source text, and a compiled or binary
# file has no line diff worth ranking.
SOURCE_EXTENSIONS = (".py", ".pyw")

# Never descended into. Version control and build output hold copies by design,
# so reporting them as drift is noise.
SKIP_DIRS = ("__pycache__", ".git", ".svn", ".hg", ".tox", ".eggs")

# Nothing larger is read, and a file over it is named as skipped rather than
# dropped in silence. Measured on the 743-file share: the largest file is a
# 10.5 MB log somebody saved with a .py extension, the largest after it is a
# 937 KB vendored library module, and nothing there reaches this limit.
READ_LIMIT = 16 * 1000 * 1000

# Shingle length for the near-duplicate similarity. Five significant tokens is
# about one short statement, which is coarse enough to survive a renamed
# variable and fine enough that two unrelated scripts do not look alike.
SHINGLE_K = 5

# Calls that destroy data. No trailing \b after a name: arcpy spells the same
# tool TruncateTable and TruncateTable_management, and underscore is a word
# character, so a trailing boundary would never match the suffixed form.
DESTRUCTIVE_RE = re.compile(
    r"rmtree"
    r"|os\.remove"
    r"|os\.unlink"
    r"|Delete_management"
    r"|DeleteFeatures"
    r"|DeleteRows"
    r"|TruncateTable"
)

# String literals that name a machine or a location on one. These are the
# values that make one copy of a script run somewhere else. There is no
# separate url alternative: a scheme always ends in a letter, so ftp:// and
# https:// already carry the letter-colon-slash that the drive-letter shape
# looks for. One put back here could never match on its own, and the assertion
# covering it could never fail.
PATHLIKE_RE = re.compile(
    r"\\\\\w"                     # UNC share
    r"|[A-Za-z]:[\\/]"            # drive letter, and every scheme:// url
    r"|\bftp\.\w"                 # bare ftp host
    r"|Database Connections"      # per-user ArcCatalog connection folder
    r"|\.(?:sde|gdb|mdb)\b",      # geodatabase or connection file
    re.IGNORECASE,
)

# What a parity table row says about its copies. A row claims "the same" when
# it says so and says nothing against it. "size-identical" counts: a claim that
# rests on size alone is exactly the claim worth testing. A row that says both
# ("in parity with each other, both differ from the archive") is not judged,
# because the tool cannot tell which pair the claim is about.
CLAIM_SAME_RE = re.compile(
    r"\b(?:identical|in parity|byte-for-byte|same|matches?|yes)\b",
    re.IGNORECASE)
CLAIM_DIFF_RE = re.compile(
    r"\b(?:not|differ(?:s|ent)?|diverge[ds]?|stale|mismatch(?:es)?|split)\b"
    r"|\bno\b(?! diff)",
    re.IGNORECASE)

# A table cell holds a path when it is a backtick span with a separator, a
# drive colon or a control byte in it. The control byte is on the list because
# a Windows path pasted through a "\a" escape loses its separator and keeps a
# BEL, and a span with no separator left must still be read as a path.
PATHISH_RE = re.compile(r"[\\/]|[\x00-\x1f\x7f]|^[A-Za-z]:")
ABSOLUTE_CELL_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")

# Path classes. See classify_path.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
PLACEHOLDER_RE = re.compile(r"<[^<>]+>|%\w+%|\$\{?\w+|[*?]")
WINDOWS_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\\w)")
DRIVE_REL_RE = re.compile(r"^[A-Za-z]:(?![\\/])")
URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+://")

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

COSMETIC = "cosmetic"
CODE = "code"
PATH = "path"
DESTRUCTIVE = "destructive"

# Ordered least to most dangerous. The ranking is the product.
CLASSES = (COSMETIC, CODE, PATH, DESTRUCTIVE)
CLASS_RANK = dict((name, i) for i, name in enumerate(CLASSES))

CLASS_SUMMARY = {
    COSMETIC: "copies differ only in comments or whitespace",
    CODE: "copies disagree about code",
    PATH: "copies name different hosts or paths",
    DESTRUCTIVE: "copies disagree about a call that destroys data",
}

# Token types that carry meaning. COMMENT and NL are deliberately absent: a
# comment edit is not a fork, and neither is a blank line.
SIGNIFICANT = (tokenize.NAME, tokenize.NUMBER, tokenize.STRING, tokenize.OP)


class Unreadable(ValueError):
    """The source could not be tokenized, so nothing can be said about it."""


# ----------------------------------------------------------------- pure core

def error_tokens(tokens):
    """ERRORTOKENs that carry real text.

    A blank one is not a finding. Python 3.9 emits an ERRORTOKEN for the run
    of whitespace in front of the character it could not read, as well as for
    the character itself, so a stream is only suspect when one of them holds
    something other than spaces.
    """
    return [tok for tok in tokens
            if tok[0] == tokenize.ERRORTOKEN and tok[1].strip()]


def token_stream(text):
    """Tokenize source text, or raise Unreadable.

    tokenize is used rather than ast on purpose. Most of a legacy ArcGIS share
    is Python 2, which ast.parse rejects outright; the tokenizer reads it, so
    those files are still grouped and ranked instead of being silently dropped.

    The ERRORTOKEN sweep is what makes the verdict the same on every supported
    interpreter. Python 3.10 and later raise TokenError on an unterminated
    string; Python 3.9, which ArcGIS Pro 3.0 to 3.2 ship, returns a stream with
    an ERRORTOKEN in it and no exception at all. Without the sweep the same
    torn file is unreadable on one interpreter and silently clean on another,
    and the promise that an unreadable copy is never called clean would hold
    on only one of them. Measured on the 743-file share: five files are
    unreadable, and they are the same five under 3.9 and under 3.13.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError,
            UnicodeDecodeError, ValueError) as exc:
        raise Unreadable("%s: %s" % (type(exc).__name__, exc))
    bad = error_tokens(tokens)
    if bad:
        raise Unreadable("ERRORTOKEN: %r at line %d"
                         % (bad[0][1], bad[0][2][0]))
    return tokens


def normalized(tokens):
    """Reduce a token stream to the tokens that make two files different.

    Indentation and newlines are kept as markers, not as text, so re-indenting
    a file does not make it a new version. Comments and blank lines are gone
    for the same reason.
    """
    out = []
    for tok in tokens:
        kind = tok[0]
        if kind in SIGNIFICANT:
            out.append(tok[1])
        elif kind == tokenize.INDENT:
            out.append("<INDENT>")
        elif kind == tokenize.DEDENT:
            out.append("<DEDENT>")
        elif kind == tokenize.NEWLINE:
            out.append("<NEWLINE>")
    return out


def digest(text):
    """Content hash of the text, as UTF-8.

    Two copies with the same digest are the same file and never need diffing.
    """
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()


def fingerprint(tokens):
    """Hash of the normalized stream. Equal fingerprints are one version."""
    joined = "\x00".join(normalized(tokens))
    return hashlib.md5(joined.encode("utf-8", "replace")).hexdigest()


def literal_text(token_string):
    """The text inside a string literal, with prefix and quotes removed.

    Quote style is not a fork. 'C:\\gis' and "C:\\gis" name the same folder, so
    they have to compare equal or every group that reformatted its quotes is
    reported as a path disagreement.
    """
    i = 0
    while i < len(token_string) and token_string[i] not in "'\"":
        i += 1
    if i == len(token_string):
        return token_string
    body = token_string[i:]
    for quote in ('"""', "'''"):
        if len(body) >= 6 and body.startswith(quote) and body.endswith(quote):
            return body[3:-3]
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "'\"":
        return body[1:-1]
    return body


def path_literals(tokens):
    """The host and path literals a file uses, as text.

    Docstrings are excluded. Esri's own vendored packages carry pages of
    reference documentation naming .sde files and http:// urls, and counting
    those made a third of the measured corpus look like it disagreed about a
    host when only the documentation had been reflowed.
    """
    found = set()
    starts_line = True
    for index, tok in enumerate(tokens):
        kind = tok[0]
        if kind in (tokenize.COMMENT, tokenize.NL):
            continue
        if kind == tokenize.STRING:
            after = tokens[index + 1][0] if index + 1 < len(tokens) else None
            is_docstring = starts_line and after == tokenize.NEWLINE
            if not is_docstring:
                value = literal_text(tok[1])
                if PATHLIKE_RE.search(value):
                    found.add(value)
        if kind in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            starts_line = True
        elif kind not in (tokenize.ENCODING, tokenize.ENDMARKER):
            starts_line = False
    return frozenset(found)


def destructive_drift(left_text, right_text):
    """Lines carrying a destructive call that the two copies disagree about.

    This reads raw lines, not tokens, because the flagship case is a line that
    one copy runs and the other has commented out. A token stream has already
    thrown the commented copy away, so the two would differ with no evidence
    left to explain why.
    """
    evidence = []
    diff = difflib.unified_diff(left_text.splitlines(),
                                right_text.splitlines(), n=0, lineterm="")
    for line in diff:
        if line[:3] in ("+++", "---") or line[:1] not in "+-":
            continue
        body = line[1:]
        if DESTRUCTIVE_RE.search(body):
            note = " [commented out]" if body.strip().startswith("#") else ""
            evidence.append("%s %s%s" % (line[:1], body.strip(), note))
    return evidence


def shingles(tokens, k=SHINGLE_K):
    """Set of overlapping k-grams over the normalized token stream."""
    words = normalized(tokens)
    if not words:
        return frozenset()
    if len(words) < k:
        return frozenset([" ".join(words)])
    return frozenset(" ".join(words[i:i + k])
                     for i in range(len(words) - k + 1))


def similarity(left, right):
    """Jaccard similarity of two shingle sets, 0.0 to 1.0.

    Two empty sets score 0.0 rather than 1.0. An empty file resembles nothing;
    calling every empty file a perfect match of every other one would fill the
    near-duplicate report with __init__.py.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


class Source(object):
    """One file reduced to everything the ranking needs."""

    def __init__(self, name, path, text, content=None):
        self.name = name.lower()
        self.path = path
        self.text = text
        self.digest = content if content is not None else digest(text)
        try:
            self.tokens = token_stream(text)
        except Unreadable as exc:
            self.tokens = []
            self.readable = False
            self.why = str(exc)
            # No token stream means no normalized form, so the raw content is
            # the only identity available. Never merge it with anything.
            self.version = self.digest
            self.shingles = frozenset()
        else:
            self.readable = True
            self.why = ""
            self.version = fingerprint(self.tokens)
            self.shingles = shingles(self.tokens)

    def __repr__(self):
        return "Source(%r, %r)" % (self.name, self.path)


class Group(object):
    """Every copy of one filename, and what the copies disagree about."""

    def __init__(self, name, sources, variants, klass, evidence):
        self.name = name
        self.sources = sources
        self.variants = variants
        self.klass = klass
        self.evidence = evidence

    @property
    def rank(self):
        return CLASS_RANK[self.klass]

    @property
    def unreadable(self):
        return [s for s in self.sources if not s.readable]

    def __repr__(self):
        return "Group(%r, %s, files=%d, variants=%d)" % (
            self.name, self.klass, len(self.sources), len(self.variants))


def classify(variants):
    """Rank what a set of distinct versions disagrees about.

    Highest class wins. A group whose copies disagree about both a print
    message and an FTP host is a host problem, because the host is the one that
    decides which machine the job talks to.
    """
    unreadable = [v for v in variants if not v.readable]
    if len(variants) < 2:
        # One version, several byte-different copies: line endings, a trailing
        # newline, a comment. Unless a copy could not be read at all, in which
        # case nothing has been proven and it must not be called clean.
        if unreadable:
            return CODE, ["%d cop(ies) could not be read" % len(unreadable)]
        return COSMETIC, []

    evidence = []
    for i in range(len(variants)):
        for j in range(i + 1, len(variants)):
            evidence.extend(destructive_drift(variants[i].text,
                                              variants[j].text))
    if evidence:
        return DESTRUCTIVE, evidence

    # Only readable variants are compared. An unreadable copy has no token
    # stream, so its literal set is empty, and comparing that against a real
    # one reports every path as "only in some versions" when the truth is that
    # one copy could not be read. That is a fabricated finding, not a measured
    # one, so the group falls through to CODE instead.
    literals = set(path_literals(v.tokens) for v in variants if v.readable)
    if len(literals) > 1:
        union = set()
        common = None
        for lit in literals:
            union |= lit
            common = lit if common is None else (common & lit)
        return PATH, ["only in some versions: %s" % lit
                      for lit in sorted(union - common)]

    return CODE, []


def analyse(sources):
    """Group sources by name and classify every name that has drifted.

    A name whose copies are all byte-identical is not drift, so it is counted
    and dropped. Only names holding two or more different contents come back.
    """
    by_name = {}
    for src in sources:
        by_name.setdefault(src.name, []).append(src)

    groups = []
    for name in sorted(by_name):
        members = by_name[name]
        if len(members) < 2:
            continue
        if len(set(s.digest for s in members)) < 2:
            continue
        variants = []
        seen = set()
        for src in members:
            if src.version not in seen:
                seen.add(src.version)
                variants.append(src)
        klass, evidence = classify(variants)
        groups.append(Group(name, members, variants, klass, evidence))

    groups.sort(key=lambda g: (-g.rank, -len(g.sources), g.name))
    return groups


def near_pairs(sources, threshold):
    """Copies that drifted across a rename, as (score, left, right).

    Only pairs with different names, because same-name pairs are already a
    group and reporting them twice buries the renames. Only one pair per pair
    of contents, because a share holding ten identical copies of a script would
    otherwise report the same rename ten times.

    ponytail: every readable pair is compared, which is O(n^2) over shingle
    sets. Measured on the 743-file share, that adds about fourteen seconds to a
    four-second scan. Before 50,000 files, prefilter on file size and on a
    shared basename stem so that most pairs are never scored at all.
    """
    readable = [s for s in sources if s.readable and s.shingles]
    pairs = []
    seen = set()
    for i in range(len(readable)):
        for j in range(i + 1, len(readable)):
            left, right = readable[i], readable[j]
            if left.name == right.name:
                continue
            contents = tuple(sorted([left.version, right.version]))
            if contents in seen:
                continue
            score = similarity(left.shingles, right.shingles)
            if score >= threshold:
                seen.add(contents)
                pairs.append((score, left, right))
    pairs.sort(key=lambda p: (-p[0], p[1].path, p[2].path))
    return pairs


def tally(sources, groups):
    """The corpus counts printed above the findings."""
    names = set(s.name for s in sources)
    counts = {}
    for src in sources:
        counts[src.digest] = counts.get(src.digest, 0) + 1
    per_class = dict((c, 0) for c in CLASSES)
    for group in groups:
        per_class[group.klass] += 1
    return {
        "files": len(sources),
        "distinct": len(counts),
        "names": len(names),
        "copied_files": sum(c for c in counts.values() if c > 1),
        "largest_identical_group": max(counts.values()) if counts else 0,
        "divergent_names": len(groups),
        "divergent_files": sum(len(g.sources) for g in groups),
        "unreadable": len([s for s in sources if not s.readable]),
        "by_class": per_class,
    }


def failing(groups, floor):
    """Groups at or above the floor class."""
    return [g for g in groups if g.rank >= CLASS_RANK[floor]]


def describe(group, show_diff=False, evidence_limit=6):
    """Render one group as the lines the CLI prints."""
    lines = ["%-11s %s  (%d copies, %d version%s)  %s" % (
        group.klass.upper(), group.name, len(group.sources),
        len(group.variants), "" if len(group.variants) == 1 else "s",
        CLASS_SUMMARY[group.klass])]
    for src in group.variants:
        lines.append("    version: %s" % src.path)
    for src in group.sources:
        if not src.readable:
            lines.append("    UNREADABLE: %s (%s)" % (src.path, src.why))
    for item in group.evidence[:evidence_limit]:
        lines.append("      %s" % item)
    if len(group.evidence) > evidence_limit:
        lines.append("      ... %d more"
                     % (len(group.evidence) - evidence_limit))
    if show_diff and len(group.variants) > 1:
        left, right = group.variants[0], group.variants[1]
        lines.append("    diff %s -> %s" % (left.path, right.path))
        diff = difflib.unified_diff(left.text.splitlines(),
                                    right.text.splitlines(), n=1, lineterm="")
        for line in list(diff)[2:22]:
            lines.append("      %s" % line)
    return lines


def as_report(counts, groups, pairs):
    """The whole result as plain data, for --out."""
    return {
        "counts": counts,
        "groups": [{
            "name": g.name,
            "class": g.klass,
            "copies": len(g.sources),
            "versions": len(g.variants),
            "paths": [s.path for s in g.sources],
            "version_paths": [s.path for s in g.variants],
            "unreadable": [s.path for s in g.unreadable],
            "evidence": g.evidence,
        } for g in groups],
        "near_duplicates": [{
            "score": round(score, 4),
            "left": left.path,
            "right": right.path,
        } for score, left, right in pairs],
    }


# --------------------------------------------------- parity table: pure core

# A parity table is a Markdown table that says two or more copies of a file are
# the same. These are the ways a path in it can fail to be checkable here.
LOCAL = "local"          # names a file this host can open
FOREIGN = "foreign"      # a real path, on another kind of host, or a url
AMBIGUOUS = "ambiguous"  # means different files depending on state not in the table
CORRUPT = "corrupt"      # holds a control byte, so it never named any file

SAME = "same"
DIFFERS = "differs"
MIXED = "mixed"
NO_CLAIM = "none"
UNPAIRED = "unpaired"

V_FALSE = "false"
V_MISSING = "missing"
V_CORRUPT = "corrupt"
V_STALE = "stale"
V_UNVERIFIABLE = "unverifiable"
V_PARTIAL = "partial"
V_HELD = "held"

# Most serious first. A row with several problems takes the first one, and the
# notes still list all of them.
VERDICTS = (V_FALSE, V_MISSING, V_CORRUPT, V_STALE, V_UNVERIFIABLE,
            V_PARTIAL, V_HELD)

VERDICT_SUMMARY = {
    V_FALSE: "the table says identical, the bytes differ",
    V_MISSING: "a copy the table names is missing or cannot be read",
    V_CORRUPT: "a path holds a control byte, so it never named a file",
    V_STALE: "a size or hash stated in the table is not what the file is now",
    V_UNVERIFIABLE: "fewer than two copies could be checked on this host",
    V_PARTIAL: "the copies checked match, the others could not be checked here",
    V_HELD: "every copy matches",
}

# Exit 1 from V_FALSE down to this one. --strict extends it to V_PARTIAL.
DEFAULT_FAIL_AT = V_STALE

# Hex length to hash name, for a digest quoted in a table cell.
DIGEST_ALGOS = {32: "md5", 40: "sha1", 64: "sha256"}
SIZE_RE = re.compile(r"(?<![\w.])(\d[\d,]*)\s*B\b")
DIGEST_RE = re.compile(r"\b(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40}|[0-9a-fA-F]{32})\b")
TICK_RE = re.compile(r"`([^`\n]+)`")

# The backslash escapes Python turns into one control byte.
ESCAPE_LETTERS = {7: "a", 8: "b", 9: "t", 10: "n", 11: "v", 12: "f", 13: "r"}


class Item(object):
    """One path a table row names, and what the row states about it."""

    def __init__(self, text, size=None, digest=None):
        self.text = text
        self.size = size
        self.digest = digest


class Entry(object):
    """A table row that claims its copies are identical."""

    def __init__(self, line, items):
        self.line = line
        self.items = items


class Finding(object):
    """What was found when one claim was checked."""

    def __init__(self, line, verdict, notes, details, paths):
        self.line = line
        self.verdict = verdict
        self.notes = notes
        self.details = details
        self.paths = paths

    @property
    def rank(self):
        return VERDICTS.index(self.verdict)


def claim_of(text):
    """What a row's words say: SAME, DIFFERS, MIXED or NO_CLAIM."""
    said_same = bool(CLAIM_SAME_RE.search(text))
    said_diff = bool(CLAIM_DIFF_RE.search(text))
    if said_same and said_diff:
        return MIXED
    if said_same:
        return SAME
    return DIFFERS if said_diff else NO_CLAIM


def split_row(line):
    """The cells of one table row, or None when the line is not a row.

    Only spaces are stripped. str.strip() with no argument also removes tab,
    vertical tab and form feed, which are the control bytes a damaged path
    ends in, and the evidence would be trimmed away before it was read.
    """
    body = line.strip(" ")
    if not body.startswith("|"):
        return None
    return [cell.strip(" ") for cell in body.strip("|").split("|")]


def is_separator(cells):
    """True for the |---|:---:| row under a table header."""
    return all(re.match(r"^:?-+:?$", cell) for cell in cells)


def cell_paths(cell):
    """The paths a cell names: backtick spans, or the whole cell if absolute.

    A span with no separator, no drive colon and no control byte is a label
    such as a file name in a "File" column, not a place.
    """
    spans = TICK_RE.findall(cell)
    if not spans and ABSOLUTE_CELL_RE.match(cell):
        spans = [cell]
    return [s for s in spans if PATHISH_RE.search(s)]


def stated_size(text):
    """A byte count a cell states, such as '33,782 B', or None."""
    found = SIZE_RE.search(text)
    return int(found.group(1).replace(",", "")) if found else None


def stated_digest(text):
    """A 32, 40 or 64 digit hex digest a cell states, or None."""
    found = DIGEST_RE.search(text)
    return found.group(0).lower() if found else None


def join_dir(directory, name):
    """directory and file name, joined with the separator the directory uses."""
    sep = "\\" if "\\" in directory else "/"
    return directory.rstrip("\\/") + sep + name


def header_dirs(cells):
    """Header cells that each name one folder: {column: folder}, or None.

    A table whose header names two folders is the one-copy-per-column shape:
    the rows hold file names, and the path is the folder joined to the name.
    """
    dirs = {}
    for col, cell in enumerate(cells):
        spans = cell_paths(cell)
        if len(spans) == 1:
            dirs[col] = spans[0]
    return dirs if len(dirs) >= 2 else None


def parse_table(text):
    """Every row that claims identical copies, and how many rows it skipped.

    Returns (entries, skipped). Rows are matched to source lines one to one,
    so a line number printed in the report is the line in the table file. The
    text is split on newlines only. str.splitlines() also splits on vertical
    tab, form feed and a few other bytes, which would tear a damaged path in
    half and hide the row.
    """
    rows = [split_row(line)
            for line in text.replace("\r\n", "\n").split("\n")]
    entries = []
    skipped = dict((key, 0) for key in (DIFFERS, MIXED, NO_CLAIM, UNPAIRED))
    dirs = None
    for index, cells in enumerate(rows):
        if cells is None:
            dirs = None
            continue
        if is_separator(cells):
            continue
        following = rows[index + 1] if index + 1 < len(rows) else None
        if following is not None and is_separator(following):
            dirs = header_dirs(cells)
            continue
        items = []
        if dirs:
            name = cells[0].strip("`* ")
            if name and not PATHISH_RE.search(name):
                for col in sorted(dirs):
                    cell = cells[col] if col < len(cells) else ""
                    items.append(Item(join_dir(dirs[col], name),
                                      stated_size(cell), stated_digest(cell)))
            said = " ".join(cells[1:])
        else:
            parts = []
            for cell in cells:
                spans = cell_paths(cell)
                rest = cell
                for span in spans:
                    rest = rest.replace("`%s`" % span, " ").replace(span, " ")
                parts.append(rest)
                for span in spans:
                    alone = len(spans) == 1
                    items.append(Item(
                        span,
                        stated_size(rest) if alone else None,
                        stated_digest(rest) if alone else None))
            said = " ".join(parts)
        claim = claim_of(said)
        if claim != SAME:
            skipped[claim] += 1
        elif len(items) < 2:
            skipped[UNPAIRED] += 1
        else:
            entries.append(Entry(index + 1, items))
    return entries, skipped


def escape_hint(text):
    """The text with each control byte written back as a backslash escape."""
    out = []
    for ch in text:
        code = ord(ch)
        if code in ESCAPE_LETTERS:
            out.append("\\" + ESCAPE_LETTERS[code])
        elif CONTROL_RE.match(ch):
            out.append("\\x%02x" % code)
        else:
            out.append(ch)
    return "".join(out)


def apply_map(text, maps):
    """text with the first matching (prefix, folder) mapping applied, or None.

    Separators are compared as one kind and case is ignored: the tables this
    reads are written on Windows, where neither matters.
    """
    flat = text.replace("\\", "/")
    for prefix, folder in maps:
        head = prefix.replace("\\", "/").rstrip("/")
        if flat.lower() == head.lower() or flat.lower().startswith(
                head.lower() + "/"):
            rest = [p for p in flat[len(head):].split("/") if p]
            return os.path.join(folder, *rest)
    return None


def classify_path(text, host, base=None, maps=()):
    """Sort one path from a table into a class: (class, local path, reason).

    host is "nt" or "posix", passed in so both can be tested anywhere.
    LOCAL means this host can open it, and the second item is where. FOREIGN is
    a real path that only another kind of host can open, or a url. AMBIGUOUS is
    a path whose target depends on state the table does not hold. CORRUPT holds
    a control byte. None of the last three is ever reported as a missing file,
    because "I could not look" is not "it is not there".
    """
    found = CONTROL_RE.search(text)
    if found:
        return CORRUPT, None, (
            "control byte 0x%02x at offset %d, read as %s"
            % (ord(found.group(0)), found.start(), ascii(escape_hint(text))))
    if PLACEHOLDER_RE.search(text):
        return AMBIGUOUS, None, "placeholder, variable or wildcard"
    if URL_RE.match(text):
        return FOREIGN, None, "a url, not a file"
    mapped = apply_map(text, maps)
    if mapped is not None:
        return LOCAL, mapped, ""
    if WINDOWS_ABS_RE.match(text):
        if host == "nt":
            return LOCAL, text, ""
        return FOREIGN, None, "a Windows path on a posix host; add --map"
    if text[:1] in ("\\", "~"):
        return AMBIGUOUS, None, "rooted or home-relative with no drive or user"
    if DRIVE_REL_RE.match(text):
        return AMBIGUOUS, None, "drive-relative: depends on the folder in effect"
    if text.startswith("/"):
        if "\\" in text:
            return AMBIGUOUS, None, (
                "a backslash in a posix path: separator or part of a name")
        if host == "posix":
            return LOCAL, text, ""
        return FOREIGN, None, "a posix path on a Windows host; add --map"
    if base is None:
        return AMBIGUOUS, None, "relative path and no --base"
    parts = [p for p in re.split(r"[\\/]", text) if p not in ("", ".")]
    return LOCAL, os.path.join(base, *parts), ""


def digest_algo(digest):
    """Hash name for a stated digest, or None when the row stated none."""
    return DIGEST_ALGOS[len(digest)] if digest else None


def judge(entry, host, base, maps, measure_fn):
    """Check one claim of identical copies. measure_fn does the reading.

    The reading is passed in so the decision is tested without a disk. A path
    that is not LOCAL is counted as unchecked, never as missing.
    """
    flags = set()
    notes = []
    details = []
    paths = []
    checked = []
    unchecked = 0
    for item in entry.items:
        klass, where, why = classify_path(item.text, host, base, maps)
        paths.append((item.text, klass, where))
        if klass == CORRUPT:
            flags.add(V_CORRUPT)
            notes.append("corrupt %s: %s" % (ascii(item.text), why))
        elif klass != LOCAL:
            unchecked += 1
            notes.append("%s %s: %s" % (klass, ascii(item.text), why))
        else:
            algo = digest_algo(item.digest)
            try:
                size, sums = measure_fn(where, [algo] if algo else [])
            except (IOError, OSError):
                flags.add(V_MISSING)
                notes.append("missing or unreadable: %s" % ascii(item.text))
                continue
            checked.append((item, size, sums))
            details.append("%s  %d B  sha256 %s"
                           % (ascii(item.text), size, sums["sha256"][:12]))

    if len(set(sums["sha256"] for _, _, sums in checked)) > 1:
        flags.add(V_FALSE)
        sizes = set(size for _, size, _ in checked)
        if len(sizes) == 1:
            notes.append("same size (%d B), different bytes: a size-only "
                         "check calls these identical" % sizes.pop())
    for item, size, sums in checked:
        algo = digest_algo(item.digest)
        if item.size is not None and item.size != size:
            flags.add(V_STALE)
            notes.append("table says %d B, %s is %d B now"
                         % (item.size, ascii(item.text), size))
        if algo and sums[algo] != item.digest:
            flags.add(V_STALE)
            notes.append("table says %s %s, %s is %s now"
                         % (algo, item.digest[:12], ascii(item.text),
                            sums[algo][:12]))

    if flags:
        verdict = min(flags, key=VERDICTS.index)
    elif len(checked) < 2:
        verdict = V_UNVERIFIABLE
    elif unchecked:
        verdict = V_PARTIAL
    else:
        verdict = V_HELD
    return Finding(entry.line, verdict, notes, details, paths)


def parity_failing(findings, strict):
    """Findings that fail the run: V_STALE and worse, V_PARTIAL with strict."""
    limit = VERDICTS.index(V_PARTIAL if strict else DEFAULT_FAIL_AT)
    return [f for f in findings if f.rank <= limit]


def parity_counts(findings):
    """How many findings carry each verdict."""
    counts = dict((v, 0) for v in VERDICTS)
    for finding in findings:
        counts[finding.verdict] += 1
    return counts


def describe_finding(finding):
    """Render one finding as the lines the CLI prints."""
    lines = ["%-12s line %d  %s" % (finding.verdict.upper(), finding.line,
                                     VERDICT_SUMMARY[finding.verdict])]
    for detail in finding.details:
        lines.append("    checked  %s" % detail)
    for note in finding.notes:
        lines.append("    %s" % note)
    return lines


def as_parity_report(table, host, skipped, findings):
    """The whole parity result as plain data, for --out."""
    return {
        "table": table,
        "host": host,
        "counts": parity_counts(findings),
        "not_judged": skipped,
        "rows": [{
            "line": f.line,
            "verdict": f.verdict,
            "notes": f.notes,
            "paths": [{"text": text, "class": klass, "local": where}
                      for text, klass, where in f.paths],
        } for f in sorted(findings, key=lambda f: (f.rank, f.line))],
    }


# ------------------------------------------------------------------ io layer

def decode(data):
    """Decode source bytes. Nothing on a legacy share declares its encoding.

    latin-1 maps every one of the 256 byte values, so it cannot fail and no
    third fallback is needed. A mis-decoded comment costs nothing here: the
    fingerprint is taken over tokens and the identity over the raw bytes.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def read_bytes(path):
    """The bytes of one file, or Unreadable when it is over the read limit."""
    handle = open(path, "rb")
    try:
        data = handle.read(READ_LIMIT + 1)
    finally:
        handle.close()
    if len(data) > READ_LIMIT:
        raise Unreadable("larger than the %d byte read limit" % READ_LIMIT)
    return data


def load(path):
    """Read one file into a Source. Hashes the bytes, not the decoded text."""
    data = read_bytes(path)
    return Source(os.path.basename(path), path, decode(data),
                  content=hashlib.md5(data).hexdigest())


def measure(path, algos=()):
    """Size and digests of one file, read in chunks. Raises OSError.

    sha256 is always taken. The others are the algorithms a parity table named
    in its own hash cells, so the table's claim can be checked as it was made.
    """
    hashers = dict((name, hashlib.new(name))
                   for name in set(algos) | set(["sha256"]))
    size = 0
    handle = open(path, "rb")
    try:
        chunk = handle.read(1 << 20)
        while chunk:
            size += len(chunk)
            for hasher in hashers.values():
                hasher.update(chunk)
            chunk = handle.read(1 << 20)
    finally:
        handle.close()
    return size, dict((name, h.hexdigest()) for name, h in hashers.items())


def walk_sources(root, extensions):
    """Every candidate file under root, or the file itself."""
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            if name.lower().endswith(tuple(extensions)):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


# ------------------------------------------------------------------ self-test

def _harness(out):
    """The check()/raises() pair, and the footer that sums them up.

    A function of its own so the self-test can drive a second copy and watch
    it fail on purpose. Without that, the FAIL branches and the failing footer
    are code that has never run, in the one place that decides whether the
    other 200 assertions count.
    """
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            out("PASS  %s" % label)
        else:
            failed.append(label)
            out("FAIL  %s" % label)

    def raises(fn, label, kind=Unreadable):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def finish():
        out("-" * 70)
        total = passed[0] + len(failed)
        if failed:
            out("%d assertions, %d failed" % (total, len(failed)))
            for item in failed:
                out("  FAILED: %s" % item)
            return 1
        out("%d assertions, 0 failed" % total)
        return 0

    return check, raises, finish


def self_test():
    """Assertions over the decision core. No share, no network, no arcpy."""
    check, raises, finish = _harness(print)

    def src(name, text):
        return Source(name, "memory/" + name, text)

    print("clonedrift self-test: no share, no network, no arcpy")
    print("-" * 70)

    # ---- fixtures, written from scratch for this test. Synthetic names only.
    base = (
        "import shutil\n"
        "import arcpy\n"
        "STAGE = 'D:\\\\staging\\\\roads'\n"
        "def run():\n"
        "    arcpy.Append_management(STAGE, 'roads', 'NO_TEST')\n"
        "    shutil.rmtree(STAGE)\n"
    )
    commented = base.replace("    shutil.rmtree(STAGE)",
                             "    # shutil.rmtree(STAGE)")
    other_host = base.replace("D:\\\\staging\\\\roads",
                              "\\\\\\\\fileserver01\\\\staging\\\\roads")
    louder = base.replace("def run():", "def run():\n    print('starting')")
    py2 = "import arcpy\nprint 'done'\n"
    torn = "msg = 'never closed\n"

    # ---- one version, several copies
    check(src("a.py", base).version == src("a.py", base + "# note\n").version,
          "a copy with an extra comment is the same version  <-- pinned defect")
    check(src("a.py", base).version == src("a.py", base.replace(
              "import arcpy", "import arcpy  # for Append")).version,
          "a copy with a trailing comment is the same version")
    check(src("a.py", base).version == src("a.py", base + "\n\n\n").version,
          "a copy with extra blank lines is the same version")
    check(src("a.py", base).version == src("a.py", base.replace(
              "    arcpy", "        arcpy").replace(
              "    shutil", "        shutil")).version,
          "a copy re-indented to 8 spaces is the same version")
    check(src("a.py", base).version == src("a.py", base.replace(
              "import arcpy\n", "import arcpy   \n")).version,
          "a copy with trailing whitespace is the same version")
    check(src("a.py", base).digest != src("a.py", base + "# note\n").digest,
          "those same two copies are still different bytes")
    check(src("a.py", base).version != src("a.py", other_host).version,
          "a copy naming another host is a second version")
    check(src("a.py", base).version != src("a.py", commented).version,
          "a copy with rmtree commented out is a second version")
    check(src("a.py", base).version == src("a.py", base).version,
          "the fingerprint is stable across two reads of one text")
    check(src("a.py", "x = 1\n-2\n").version != src("a.py", "x = 1 - 2\n").version,
          "a statement boundary is part of the version  <-- pinned defect")

    # ---- what the tokenizer can and cannot read
    check(src("p2.py", py2).readable,
          "a Python 2 print statement is read, not skipped  <-- pinned defect")
    check(src("p2.py", py2).version == src("p2.py", py2 + "# tail\n").version,
          "a Python 2 file is fingerprinted like any other")
    check(src("p2.py", "try:\n    f()\nexcept IOError, e:\n    pass\n").readable,
          "a Python 2 except clause is read, not skipped")
    bad = src("bad.py", torn)
    check(not bad.readable, "an unterminated string is unreadable")
    check(bad.version == bad.digest,
          "an unreadable copy falls back to its own content hash")
    check(bad.shingles == frozenset(),
          "an unreadable copy has no shingles to compare")
    raises(lambda: token_stream(torn),
           "tokenizing an unterminated string raises Unreadable")
    raises(lambda: token_stream("rows = arcpy.Append(a,\n"),
           "tokenizing an unclosed bracket raises Unreadable")

    # The ERRORTOKEN sweep, on hand-built tokens. Which source text produces an
    # ERRORTOKEN is an interpreter detail: 3.9 emits one for an unterminated
    # string that 3.13 raises on instead. The predicate is asserted directly so
    # this case is pinned on every interpreter rather than on one of them.
    err = (tokenize.ERRORTOKEN, "'", (1, 6), (1, 7), "msg = '")
    gap = (tokenize.ERRORTOKEN, "   ", (1, 3), (1, 6), "msg = '")
    name = (tokenize.NAME, "msg", (1, 0), (1, 3), "msg = '")
    check(error_tokens([name, err]) == [err],
          "an ERRORTOKEN holding real text is an error  <-- pinned defect")
    check(error_tokens([name, gap]) == [],
          "an ERRORTOKEN holding only spaces is not  <-- pinned defect")
    check(error_tokens([name]) == [],
          "a clean stream has no error tokens")

    # ---- path literals
    def lits(text):
        return path_literals(token_stream(text))

    check(lits("p = '\\\\\\\\fileserver01\\\\staging'")
          == frozenset(["\\\\\\\\fileserver01\\\\staging"]),
          "a UNC literal is a path literal, escapes and all")
    # One shape per assertion. The earlier fixtures matched two alternatives
    # at once: 'D:\\staging' as written also carries the UNC backslash pair,
    # and every scheme:// url carries a letter-colon-slash. Removing either
    # alternative from PATHLIKE_RE left those assertions green.
    check(lits("p = 'D:/staging'") == frozenset(["D:/staging"]),
          "a drive letter literal is a path literal  <-- pinned defect")
    check("ftp://vendor.example.net" in lits("h = 'ftp://vendor.example.net'"),
          "an ftp url literal is a path literal, by the same shape")
    check("ftp.example.net" in lits("h = 'ftp.example.net'"),
          "a bare ftp host literal is a path literal")
    check(lits("c = 'Database Connections/prod'")
          == frozenset(["Database Connections/prod"]),
          "an ArcCatalog connection literal is a path literal"
          "  <-- pinned defect")
    check(lits("g = 'roads.gdb'") == frozenset(["roads.gdb"]),
          "a geodatabase name on its own is a path literal"
          "  <-- pinned defect")
    check(lits("s = 'gis.sde'") == frozenset(["gis.sde"]),
          "a connection file name on its own is a path literal")
    check(lits("h = 'gisprod01'") == frozenset(),
          "a bare hostname with no path syntax is not recognised")
    check(lits("f = 'AddressExtract_1.csv'") == frozenset(),
          "a file name that is not a geodatabase is not a path literal")
    check(lits("f = 'summary.gdbx'") == frozenset(),
          "an extension that only starts like .gdb is not a path literal")
    check(lits("msg = 'update finished'") == frozenset(),
          "a plain message is not a path literal")
    check(lits('"""Reads D:\\\\gis\\\\roads.gdb."""\nx = 1\n') == frozenset(),
          "a module docstring naming a gdb is not a path literal"
          "  <-- pinned defect")
    check(lits("def f():\n    '''Writes gis.sde.'''\n    return 1\n")
          == frozenset(),
          "a function docstring naming an sde is not a path literal"
          "  <-- pinned defect")
    check(lits("# p = 'D:\\\\staging'\nx = 1\n") == frozenset(),
          "a commented-out path is not a path literal")
    check(lits("p = 'D:\\\\staging'") == lits('p = "D:\\\\staging"'),
          "the same path in single and double quotes is one literal"
          "  <-- pinned defect")
    check(literal_text("r'D:\\\\gis'") == literal_text("'D:\\\\gis'"),
          "a raw prefix does not change the literal text")
    check(literal_text('"""doc"""') == "doc",
          "a triple-quoted literal loses exactly its quotes")
    check(literal_text("unquoted") == "unquoted",
          "text with no quotes at all is returned unchanged")
    check(literal_text("'unbalanced") == "'unbalanced",
          "an unbalanced quote is returned unchanged rather than sliced")
    check("etl.py" in repr(src("etl.py", base)),
          "a source prints its own name")

    # ---- destructive drift
    check(destructive_drift(base, commented),
          "rmtree live in one copy and commented in the other is drift"
          "  <-- pinned defect")
    check(any("[commented out]" in e
              for e in destructive_drift(base, commented)),
          "the evidence says which side has it commented out")
    check(destructive_drift(commented, commented) == [],
          "two copies that both comment it out do not disagree")
    check(destructive_drift(base, louder) == [],
          "an added print is not destructive drift")
    check(DESTRUCTIVE_RE.search("arcpy.TruncateTable_management(t)"),
          "TruncateTable matches despite the _management suffix"
          "  <-- pinned defect")
    for call in ("shutil.rmtree(d)", "os.remove(f)", "os.unlink(f)",
                 "arcpy.Delete_management(fc)", "arcpy.DeleteFeatures(fc)",
                 "arcpy.DeleteRows(t)"):
        check(DESTRUCTIVE_RE.search(call),
              "the destructive list matches %s" % call.split("(")[0])
    check(not DESTRUCTIVE_RE.search("arcpy.Append_management(a, b, 'NO_TEST')"),
          "an append is not on the destructive list")
    check(not DESTRUCTIVE_RE.search("shutil.copytree(a, b)"),
          "a copy is not on the destructive list")
    check(DESTRUCTIVE_RE.search("arcpy.TruncateTable(t)"),
          "TruncateTable matches without the suffix too")
    check(destructive_drift("arcpy.TruncateTable('roads')\n",
                            "arcpy.TruncateTable('trails')\n"),
          "a destructive call whose target changed is drift")
    check(destructive_drift("x = 1\n", "x = 2\n") == [],
          "an ordinary edit is not destructive drift")

    # ---- classification and its ranking
    check(classify([src("a.py", base)])[0] == COSMETIC,
          "one surviving version classifies cosmetic")
    check(classify([src("a.py", base), src("a.py", commented)])[0]
          == DESTRUCTIVE,
          "a commented-out rmtree classifies destructive")
    check(classify([src("a.py", base), src("a.py", other_host)])[0] == PATH,
          "a different host classifies path")
    check(classify([src("a.py", base), src("a.py", louder)])[0] == CODE,
          "an added print classifies code")
    both = classify([src("a.py", other_host), src("a.py", louder)])
    check(both[0] == PATH,
          "a host disagreement outranks a print message  <-- pinned defect")
    check(any("fileserver01" in e for e in both[1]),
          "the path evidence names the literal that differs")
    check(all(e.startswith("only in some versions:") for e in both[1]),
          "the path evidence says the literal is not in every version")
    shared_a = base.replace("import shutil\n",
                            "import shutil\nARCHIVE = 'D:\\\\hold\\\\a'\n")
    shared_b = base.replace("import shutil\n",
                            "import shutil\nARCHIVE = 'D:\\\\hold\\\\b'\n")
    shared = classify([src("a.py", shared_a), src("a.py", shared_b)])
    check(shared[0] == PATH, "two copies holding different archive folders is path drift")
    check(len(shared[1]) == 2,
          "only the two literals that differ are offered as evidence")
    check(not any("staging" in e for e in shared[1]),
          "a path literal both versions share is not evidence  <-- pinned defect")
    check(classify([src("a.py", other_host), src("a.py", commented)])[0]
          == DESTRUCTIVE,
          "a destructive flip outranks a host disagreement")
    mixed = classify([src("a.py", base), src("a.py", commented),
                      src("a.py", torn)])
    check(mixed[0] == DESTRUCTIVE,
          "an unreadable copy beside two real versions still ranks the versions")
    plain = base.replace("    shutil.rmtree(STAGE)\n", "")
    guessed = classify([src("a.py", plain), src("a.py", torn)])
    check(guessed[0] == CODE,
          "an unreadable copy never manufactures a path disagreement"
          "  <-- pinned defect")
    check(not any("staging" in e for e in guessed[1]),
          "and no literal is offered as evidence against a copy never read"
          "  <-- pinned defect")
    lone = classify([src("bad.py", torn)])
    check(lone[0] == CODE,
          "a lone unreadable copy is never called cosmetic  <-- pinned defect")
    check(any("could not be read" in e for e in lone[1]),
          "that verdict says the copy could not be read")
    check(CLASS_RANK[DESTRUCTIVE] > CLASS_RANK[PATH] > CLASS_RANK[CODE]
          > CLASS_RANK[COSMETIC],
          "the four classes rank destructive over path over code over cosmetic")

    # ---- grouping
    three = analyse([src("etl.py", base), src("etl.py", base),
                     src("etl.py", commented)])
    check(len(three) == 1, "A==B and B!=C is one group")
    check(len(three[0].sources) == 3, "that group holds all three copies")
    check(len(three[0].variants) == 2,
          "that group holds two versions, not three  <-- pinned defect")
    check(three[0].klass == DESTRUCTIVE, "and it is ranked destructive")
    check(analyse([src("etl.py", base)]) == [],
          "a name with one copy is not a group")
    check(analyse([src("etl.py", base), src("etl.py", base)]) == [],
          "byte-identical copies are not a divergent group")
    cased = analyse([src("ETL.py", base), src("etl.py", commented)])
    check(len(cased) == 1,
          "copies whose names differ only in case group together"
          "  <-- pinned defect")
    check(cased[0].name == "etl.py", "the group is named in lower case")
    ordered = analyse([src("a.py", base), src("a.py", louder),
                       src("z.py", base), src("z.py", commented)])
    check([g.name for g in ordered] == ["z.py", "a.py"],
          "the most dangerous group is reported first")
    check(analyse([]) == [], "an empty tree produces no groups")
    check("destructive" in repr(three[0]),
          "a group prints its own class")
    unread_group = analyse([src("u.py", base), src("u.py", base + " \n"),
                            src("u.py", torn)])
    check(unread_group[0].unreadable,
          "a group remembers which copies could not be read")

    # ---- similarity
    a_shingles = src("a.py", base).shingles
    b_shingles = src("b.py", louder).shingles
    check(similarity(a_shingles, b_shingles)
          == similarity(b_shingles, a_shingles),
          "similarity is symmetric  <-- pinned defect")
    check(similarity(a_shingles, a_shingles) == 1.0,
          "a shingle set matches itself exactly")
    check(0.0 < similarity(a_shingles, b_shingles) < 1.0,
          "a near copy scores between nothing and everything")
    check(similarity(frozenset(["a b c d e"]),
                     frozenset(["v w x y z"])) == 0.0,
          "two unrelated sets score zero")
    check(similarity(frozenset(), frozenset()) == 0.0,
          "two empty sets score zero, not one  <-- pinned defect")
    check(similarity(a_shingles, frozenset()) == 0.0,
          "anything against an empty set scores zero")
    check(len(shingles(token_stream("x = 1\n"))) == 1,
          "a file shorter than one shingle still has one")
    check(shingles(token_stream("")) == frozenset(),
          "an empty file has no shingles")
    renamed = [src("roadsync.py", base), src("roadsync_old.py", louder),
               src("unrelated.py", "import json\nprint(json.dumps({}))\n")]
    pairs = near_pairs(renamed, 0.5)
    check(len(pairs) == 1, "a renamed copy is found across two names")
    check(pairs[0][1].name != pairs[0][2].name,
          "a file is never paired with its own name  <-- pinned defect")
    check(near_pairs([src("a.py", base), src("a.py", louder)], 0.1) == [],
          "two copies of one name are left to their group, not paired")
    check(near_pairs(renamed, 0.99) == [],
          "a threshold above the score reports nothing")
    check(near_pairs([src("a.py", torn), src("b.py", torn)], 0.0) == [],
          "an unreadable file is never near anything")
    many = [src("roadsync.py", base), src("nightly.py", base),
            src("nightly_old.py", base), src("other.py", louder)]
    check(len(near_pairs(many, 0.5)) == 2,
          "three copies of one content report two content pairs, not five"
          "  <-- pinned defect")
    check(len(near_pairs(renamed + [src("third.py", commented)], 0.5)) == 2,
          "a third distinct content adds its own pair")

    # ---- counts and the gate
    corpus = [src("etl.py", base), src("etl.py", base),
              src("etl.py", commented), src("solo.py", louder)]
    groups = analyse(corpus)
    counts = tally(corpus, groups)
    check(counts["files"] == 4, "the tally counts every file")
    check(counts["distinct"] == 3, "the tally counts distinct contents")
    check(counts["names"] == 2, "the tally counts distinct names")
    check(counts["copied_files"] == 2,
          "the tally counts the files sitting in an identical group")
    check(counts["largest_identical_group"] == 2,
          "the tally names the largest identical group")
    check(counts["divergent_names"] == 1, "the tally counts divergent names")
    check(counts["divergent_files"] == 3,
          "the tally counts the files inside those names")
    check(counts["by_class"][DESTRUCTIVE] == 1,
          "the tally breaks the groups down by class")
    check(tally([], [])["largest_identical_group"] == 0,
          "an empty scan reports no identical group rather than failing")
    check(len(failing(groups, DESTRUCTIVE)) == 1,
          "a destructive group fails a destructive floor")
    check(len(failing(groups, COSMETIC)) == 1,
          "it fails a cosmetic floor too")
    check(failing(analyse([src("a.py", base), src("a.py", base + "# x\n")]),
                  CODE) == [],
          "a cosmetic group passes a code floor")
    report = as_report(counts, groups, near_pairs(corpus, 0.99))
    check(json.loads(json.dumps(report))["counts"]["files"] == 4,
          "the report survives a round trip through JSON")
    check(report["groups"][0]["evidence"],
          "the report carries the evidence with the group")
    check(report["near_duplicates"] == [],
          "a scan with no rename reports no near duplicates")

    # ---- rendering
    rendered = describe(groups[0])
    check(rendered[0].startswith("DESTRUCTIVE"),
          "a rendered group leads with its class")
    check("2 versions" in rendered[0],
          "a rendered group counts its versions")
    single = analyse([src("c.py", base), src("c.py", base + "# tail\n")])[0]
    check("1 version)" in describe(single)[0],
          "one version is not printed as '1 versions'")
    check(any("rmtree" in line for line in rendered),
          "a rendered destructive group shows the disputed call")
    check(any("UNREADABLE" in line for line in describe(unread_group[0])),
          "a rendered group names the copy it could not read")
    check(any("diff " in line
              for line in describe(groups[0], show_diff=True)),
          "--show-diff adds the diff between the first two versions")
    check(len(describe(groups[0]))
          < len(describe(groups[0], show_diff=True)),
          "--show-diff is strictly more output")
    long_group = analyse([src("m.py", base), src("m.py", commented)])[0]
    long_group.evidence = ["e%d" % i for i in range(9)]
    check(any("... 3 more" in line for line in describe(long_group)),
          "evidence past the limit is counted rather than printed")

    # ---- the harness itself, driven on purpose to fail
    sink = []
    probe_check, probe_raises, probe_finish = _harness(sink.append)
    probe_check(True, "kept")
    probe_check(False, "broken on purpose")
    probe_raises(lambda: 1 // 0, "wrong kind of error")
    probe_raises(lambda: None, "no error at all")
    probe_raises(lambda: token_stream("msg = 'never closed\n"), "right error")
    check(probe_finish() == 1 and "5 assertions, 3 failed" in sink
          and "  FAILED: broken on purpose" in sink
          and "FAIL  broken on purpose" in sink,
          "a failing assertion fails the footer and is named  <-- pinned defect")
    check(any("wrong kind of error (wrong exception" in line for line in sink)
          and any("no error at all (no error raised)" in line for line in sink),
          "raises() names a wrong exception and a missing one apart")
    sink2 = []
    clean_check, _, clean_finish = _harness(sink2.append)
    clean_check(True, "kept")
    check(clean_finish() == 0 and sink2[-1] == "1 assertions, 0 failed",
          "a run with no failure returns 0 and says so")

    # An ERRORTOKEN is an interpreter detail: 3.9 emits one for a torn string,
    # 3.13 raises. The tokenizer is replaced so the sweep runs on both.
    real_tokens = tokenize.generate_tokens
    tokenize.generate_tokens = lambda readline: iter(
        [(tokenize.ERRORTOKEN, "'", (3, 6), (3, 7), "msg = '")])
    swept = ""
    try:
        try:
            token_stream("msg = 'x")
        except Unreadable as exc:
            swept = str(exc)
    finally:
        tokenize.generate_tokens = real_tokens
    check("ERRORTOKEN" in swept and "line 3" in swept,
          "an ERRORTOKEN in the stream makes the file unreadable on any"
          " interpreter  <-- pinned defect")

    # ---- parity table: what a row claims
    check(claim_of("size-identical, 158 ms apart") == SAME,
          "a claim that rests on size alone is still a claim of identical")
    check(claim_of("**YES** - byte-for-byte diff confirmed identical") == SAME,
          "a diff that confirmed identical is a claim of identical"
          "  <-- pinned defect")
    check(claim_of("identical, no diff") == SAME,
          "'no diff' is not a denial  <-- pinned defect")
    check(claim_of("**NO** - diverged by design") == DIFFERS,
          "a row that says NO is a claim of difference")
    check(claim_of("in parity with each other; both differ from the frozen"
                   " copy") == MIXED,
          "a row that says both is mixed, and is not judged")
    check(claim_of("pending") == NO_CLAIM, "a row that says nothing is no claim")

    # ---- parity table: cells
    check(split_row("| a\t| b |") == ["a\t", "b"],
          "a tab at the end of a cell is kept, not trimmed away")
    check(split_row("not a row") is None, "a line without a leading bar is no row")
    check(is_separator(["---", ":---:", "--:"]), "a rule row is a separator")
    check(not is_separator(["---", "x"]), "a data row is not a separator")
    check(cell_paths("`a/b.txt` (3 B)") == ["a/b.txt"],
          "a backtick span with a separator is a path")
    check(cell_paths("`b.txt`") == [],
          "a bare file name in backticks is a label, not a place")
    check(cell_paths("/srv/x/b.txt") == ["/srv/x/b.txt"],
          "a cell that is an absolute path is a path")
    check(cell_paths("n/a") == [], "words around a slash are not a path")
    check(cell_paths("`\x07rchive`") == ["\x07rchive"],
          "a span that is only a BEL and a word is still a path"
          "  <-- pinned defect")
    check(split_row("\t| a |") is None,
          "a row indented with a tab is not a table row")
    check(cell_paths("`C:\x07rcgisserver`") == ["C:\x07rcgisserver"],
          "a span with only a drive colon and a BEL is still a path"
          "  <-- pinned defect")
    check(stated_size("(33,782 B, 2026-08-05)") == 33782,
          "a size with a thousands comma is read")
    check(stated_size("12 KB") is None and stated_size("v2.3 B") is None,
          "a KB figure and a version number are not a byte count")
    check(stated_digest("md5 " + "AB" * 16) == "ab" * 16,
          "a digest is read and folded to lower case")
    check(stated_digest("ab" * 15 + "a") is None,
          "thirty-one hex digits is not a digest")
    check(join_dir("/a/", "x") == "/a/x" and join_dir("C:\\d\\", "x") == "C:\\d\\x"
          and join_dir("rel", "x") == "rel/x",
          "a folder and a name join with the folder's own separator")
    check(header_dirs(["File", "`/a/`", "`/b/`", "Identical?"])
          == {1: "/a/", 2: "/b/"},
          "a header naming two folders gives a column for each")
    check(header_dirs(["File", "Copy A", "`/b/`"]) is None,
          "a header naming one folder is not the folder-per-column shape")

    # ---- parity table: whole tables
    shape_a = (
        "| File | Copy A | Copy B | Identical? |\n"
        "|---|---|---|---|\n"
        "| a | `/a/x.sql` (3 B) | `/b/x.sql` (3 B) | **YES** - byte-for-byte |\n"
        "| b | `/a/y` | `/b/y` | **NO** - diverged by design |\n"
        "| c | `/a/z` | `/b/z` | in parity with each other; both differ |\n"
        "| d | `/a/w` | `/b/w` | pending |\n"
        "| e | `/a/v` | | identical |\n")
    entries, skipped = parse_table(shape_a)
    check([e.line for e in entries] == [3], "only the row that claims identical"
          " is kept, and its line number is the table's own")
    check([i.text for i in entries[0].items] == ["/a/x.sql", "/b/x.sql"],
          "the two paths of a row are read in order")
    check([i.size for i in entries[0].items] == [3, 3],
          "the byte count stated beside each path is attached to it")
    check(skipped == {DIFFERS: 1, MIXED: 1, NO_CLAIM: 1, UNPAIRED: 1},
          "every skipped row is counted under its reason")
    check(parse_table(shape_a.replace("\n", "\r\n"))[0][0].line == 3,
          "a table saved with Windows line endings reads the same")
    shape_b = (
        "| File | `/a/dir/` | `C:\\b\\dir` | Identical? |\n"
        "|---|---|---|---|\n"
        "| one.txt | 3 B | 3 B | size-identical |\n"
        "| two.txt | 3 B (" + "AA" * 16 + ") | 3 B | yes |\n"
        "| `/odd/three.txt` | 3 B | 3 B | yes |\n"
        "\n"
        "| p.txt | `/c/p` | `/d/p` | identical |\n")
    entries, skipped = parse_table(shape_b)
    check([e.line for e in entries] == [3, 4, 7],
          "a folder-per-column table and a plain table after it both read")
    check([i.text for i in entries[0].items] == ["/a/dir/one.txt", "C:\\b\\dir\\one.txt"],
          "each file name is joined to the folder of its column")
    check(entries[1].items[0].digest == "aa" * 16 and entries[1].items[1].digest is None,
          "a digest stated in a column cell belongs to that column's copy")
    check([i.text for i in entries[2].items] == ["/c/p", "/d/p"],
          "a header's folders do not leak into the table after it"
          "  <-- pinned defect")
    check(skipped[UNPAIRED] == 1,
          "a row whose first cell is itself a path is counted, not guessed")
    damaged = ("| x | `C:\x0colder\\a.txt` | `C:\x07rcgisserver\\a.txt` |"
               " identical |\n")
    entries, _ = parse_table(damaged)
    check(len(entries) == 1 and len(entries[0].items) == 2,
          "a form feed or BEL inside a path does not tear the row"
          "  <-- pinned defect")
    check(entries[0].items[0].text == "C:\x0colder\\a.txt",
          "and the path keeps the byte, so the report can name it")
    torn_row, torn_skipped = parse_table(
        "| x | `C:\new.txt` | `/b/new.txt` | identical |\n")
    check(torn_row == [] and torn_skipped[NO_CLAIM] == 1,
          "a line feed inside a path tears the row, and the half is counted")

    # ---- path classes
    def cls(text, host, base=None, maps=()):
        return classify_path(text, host, base, maps)

    check(cls("C:\\gis\\a.txt", "nt")[:2] == (LOCAL, "C:\\gis\\a.txt"),
          "a drive path is local on a Windows host")
    check(cls("C:\\gis\\a.txt", "posix")[0] == FOREIGN,
          "a drive path is foreign on a posix host, not missing"
          "  <-- pinned defect")
    check(cls("\\\\srv\\share\\a", "posix")[0] == FOREIGN
          and cls("\\\\srv\\share\\a", "nt")[0] == LOCAL,
          "a UNC path is foreign on posix and local on Windows")
    check(cls("\\\\srv\\c$\\a", "nt")[0] == LOCAL,
          "an admin share is not mistaken for a variable  <-- pinned defect")
    check(cls("/srv/a", "posix")[:2] == (LOCAL, "/srv/a")
          and cls("/srv/a", "nt")[0] == FOREIGN,
          "a posix path is local on posix and foreign on Windows")
    klass, where, why = cls("https://host.example/a.zip", "posix")
    check(klass == FOREIGN and "url" in why,
          "a url is foreign on every host, and says it is a url")
    klass, where, why = cls("C:\x07rcgisserver", "nt")
    check(klass == CORRUPT and where is None,
          "a path with a BEL is corrupt, never a missing file"
          "  <-- pinned defect")
    check("0x07" in why and "offset 2" in why and "C:\\\\arcgisserver" in why,
          "the reason names the byte, where it sits and the path it replaced")
    check(cls("C:\x07rcgisserver", "nt", None,
              [("C:", "/mnt/c")])[0] == CORRUPT,
          "a mapping never rescues a corrupt path")
    check(escape_hint("a\x08b\x0cc\x1fd\x7fe\tf") == "a\\bb\\fc\\x1fd\\x7fe\\tf",
          "control bytes are written back as their escapes, or as hex")
    for text in ("<HOST>\\a", "%TEMP%\\a", "$HOME/a", "${HOME}/a", "/data/*.csv",
                 "/data/a?.csv"):
        check(cls(text, "posix")[0] == AMBIGUOUS,
              "%s is ambiguous: placeholder, variable or wildcard" % text)
    check(cls("\\Users\\a", "nt")[0] == AMBIGUOUS,
          "a rooted path with no drive is ambiguous")
    check(cls("~/a", "posix")[0] == AMBIGUOUS, "a home-relative path is ambiguous")
    check(cls("C:docs\\a", "nt")[0] == AMBIGUOUS
          and "drive-relative" in cls("C:docs\\a", "nt")[2],
          "a drive-relative path is ambiguous  <-- pinned defect")
    check(cls("C:docs\\a", "nt", "C:\\base")[0] == AMBIGUOUS,
          "and --base does not make a drive-relative path local")
    check(cls("/srv/a\\b", "posix")[0] == AMBIGUOUS,
          "a backslash in a posix path is ambiguous")
    check(cls("docs/a.txt", "posix")[0] == AMBIGUOUS
          and "--base" in cls("docs/a.txt", "posix")[2],
          "a relative path with no --base is ambiguous and says why")
    check(cls("docs\\sub/a.txt", "posix", "/base")[:2]
          == (LOCAL, os.path.join("/base", "docs", "sub", "a.txt")),
          "a relative path is read against --base with either separator")
    check(cls("./a.txt", "posix", "/base")[1] == os.path.join("/base", "a.txt"),
          "a leading dot segment is dropped")
    check(apply_map("C:\\work\\a\\b.txt", []) is None,
          "no mappings maps nothing")
    check(cls("C:\\work\\a\\b.txt", "posix", None,
              [("C:\\work", "/mnt/w")])[:2]
          == (LOCAL, os.path.join("/mnt/w", "a", "b.txt")),
          "a mapping turns a foreign path into a local one")
    check(cls("c:/WORK/a", "posix", None, [("C:\\work", "/mnt/w")])[0] == LOCAL,
          "a mapping ignores case and separator style")
    check(cls("C:\\work", "posix", None, [("C:\\work", "/mnt/w")])[1] == "/mnt/w",
          "a path that is exactly the prefix maps to the folder")
    check(cls("c:\\WORK", "posix", None, [("C:\\work", "/mnt/w")])[1] == "/mnt/w",
          "and a prefix in another case still matches exactly")
    check(cls("C:\\workshop\\a", "posix", None,
              [("C:\\work", "/mnt/w")])[0] == FOREIGN,
          "a prefix matches whole folder names only  <-- pinned defect")

    # ---- judging one claim, with the reading replaced by a dict
    store = {}

    def fake(path, algos):
        key = path.replace("\\", "/")
        if key not in store:
            raise IOError("no such file")
        data = store[key]
        return len(data), dict(
            (name, hashlib.new(name, data).hexdigest())
            for name in ("sha256", "md5", "sha1"))

    def ent(*texts):
        return Entry(7, [Item(t) for t in texts])

    def verdict_of(entry, host="posix", base=None, maps=()):
        return judge(entry, host, base, maps, fake)

    store.update({"/a/x": b"abc", "/b/x": b"abc", "/c/x": b"abc",
                  "/a/y": b"abc", "/b/y": b"abd", "/a/z": b"abc",
                  "/b/z": b"abcd"})
    check(verdict_of(ent("/a/x", "/b/x")).verdict == V_HELD,
          "two copies with the same bytes hold")
    check(verdict_of(ent("/a/x", "/b/x", "/c/x")).verdict == V_HELD,
          "three copies with the same bytes hold")
    same_size = verdict_of(ent("/a/y", "/b/y"))
    check(same_size.verdict == V_FALSE,
          "same size and different bytes is false  <-- pinned defect")
    check(any("same size (3 B)" in n for n in same_size.notes),
          "and the note says a size-only check would have passed it")
    other_size = verdict_of(ent("/a/z", "/b/z"))
    check(other_size.verdict == V_FALSE
          and not any("same size" in n for n in other_size.notes),
          "different sizes are false without the same-size note")
    check(len(same_size.details) == 2 and "sha256" in same_size.details[0],
          "each copy that was read is listed with its size and hash")
    gone = verdict_of(ent("/a/x", "/nowhere/x"))
    check(gone.verdict == V_MISSING and "missing or unreadable" in gone.notes[0],
          "a named copy that cannot be opened is missing")
    top = verdict_of(ent("/a/y", "/b/y", "/nowhere/x"))
    check(top.verdict == V_FALSE and len(top.notes) == 2,
          "a row with two problems takes the worse verdict and lists both")
    part = verdict_of(ent("/a/x", "/b/x", "C:\\gis\\x"))
    check(part.verdict == V_PARTIAL and "foreign" in part.notes[0],
          "matching copies plus one foreign path is partial, not held")
    thin = verdict_of(ent("/a/x", "C:\\gis\\x"))
    check(thin.verdict == V_UNVERIFIABLE,
          "one local copy and one foreign is unverifiable, not missing"
          "  <-- pinned defect")
    check(verdict_of(ent("<HOST>/x", "/a/x")).verdict == V_UNVERIFIABLE,
          "a placeholder copy is unverifiable, not missing")
    bel = verdict_of(ent("/a/x", "C:\x07rcgisserver\\x"))
    check(bel.verdict == V_CORRUPT and "corrupt" in bel.notes[0],
          "a BEL in a path is corrupt, and is not called missing")
    mapped = verdict_of(ent("/a/x", "C:\\gis\\x"), maps=[("C:\\gis", "/b")])
    check(mapped.verdict == V_HELD,
          "a mapping makes a foreign copy checkable")
    rel = verdict_of(ent("a/x", "b/x"), base="/")
    check(rel.verdict == V_HELD, "relative paths are checked against --base")
    odd = verdict_of(ent("/a/caf\u00e9", "/a/x"))
    check(odd.verdict == V_MISSING and "\\xe9" in odd.notes[0]
          and all(ord(c) < 128 for n in odd.notes for c in n),
          "a non-ASCII path is escaped in the report")

    sized = Entry(1, [Item("/a/x", size=3), Item("/b/x", size=3)])
    check(verdict_of(sized).verdict == V_HELD, "a stated size that is right holds")
    stale = Entry(1, [Item("/a/x", size=99), Item("/b/x", size=3)])
    check(verdict_of(stale).verdict == V_STALE
          and "table says 99 B" in verdict_of(stale).notes[0],
          "a stated size that is wrong makes the claim stale")
    md5_abc = hashlib.md5(b"abc").hexdigest()
    sha1_abc = hashlib.sha1(b"abc").hexdigest()
    sha256_abc = hashlib.sha256(b"abc").hexdigest()
    check([digest_algo(d) for d in (md5_abc, sha1_abc, sha256_abc, None)]
          == ["md5", "sha1", "sha256", None],
          "a stated digest's length names its algorithm")
    for good in (md5_abc, sha1_abc, sha256_abc):
        check(verdict_of(Entry(1, [Item("/a/x", digest=good),
                                   Item("/b/x")])).verdict == V_HELD,
              "a stated %d digit digest that is right holds" % len(good))
    wrong = verdict_of(Entry(1, [Item("/a/x", digest="0" * 32), Item("/b/x")]))
    check(wrong.verdict == V_STALE and "table says md5" in wrong.notes[0],
          "a stated digest that is wrong makes the claim stale")

    made = [same_size, verdict_of(ent("/a/x", "/b/x")), verdict_of(stale),
            part, thin]
    check([f.verdict for f in parity_failing(made, False)] == [V_FALSE, V_STALE],
          "false and stale fail a default run, partial and unverifiable do not")
    check([f.verdict for f in parity_failing(made, True)]
          == [V_FALSE, V_STALE, V_PARTIAL, V_UNVERIFIABLE],
          "--strict also fails partial and unverifiable, and never held")
    counts_p = parity_counts(made)
    check(counts_p[V_FALSE] == 1 and counts_p[V_HELD] == 1
          and sum(counts_p.values()) == 5,
          "the verdict tally counts every finding once")
    shown = describe_finding(same_size)
    check(shown[0].startswith("FALSE") and "line 7" in shown[0]
          and any("checked  " in line for line in shown)
          and any("same size" in line for line in shown),
          "a rendered finding leads with its verdict and shows its evidence")
    report_p = as_parity_report("t.md", "posix", {DIFFERS: 0}, made)
    check(json.loads(json.dumps(report_p))["counts"][V_FALSE] == 1
          and report_p["rows"][0]["verdict"] == V_FALSE,
          "the parity report survives JSON and lists the worst row first")
    check(report_p["rows"][0]["paths"][0]["class"] == LOCAL,
          "the report carries each path's class")
    check(VERDICTS.index(V_FALSE) < VERDICTS.index(V_MISSING)
          < VERDICTS.index(V_CORRUPT) < VERDICTS.index(V_STALE)
          < VERDICTS.index(V_UNVERIFIABLE) < VERDICTS.index(V_PARTIAL)
          < VERDICTS.index(V_HELD),
          "the verdicts rank false over missing over corrupt over stale")
    check(Finding(1, V_HELD, [], [], []).rank == len(VERDICTS) - 1,
          "a held finding ranks last")

    # ---- the filesystem and the command line, in a temporary tree
    global READ_LIMIT

    def run_main(argv):
        """Call main with stdout captured. Returns (exit code, output)."""
        buffer = io.StringIO()
        keep = sys.stdout
        sys.stdout = buffer
        try:
            code = main(argv)
        finally:
            sys.stdout = keep
        return code, buffer.getvalue()

    def write(path, data):
        handle = open(path, "wb")
        try:
            handle.write(data)
        finally:
            handle.close()

    tree = tempfile.mkdtemp(prefix="clonedrift-selftest-")
    try:
        os.mkdir(os.path.join(tree, "scheduled"))
        os.mkdir(os.path.join(tree, "archive"))
        os.mkdir(os.path.join(tree, "__pycache__"))
        write(os.path.join(tree, "scheduled", "etl.py"), base.encode("utf-8"))
        write(os.path.join(tree, "archive", "ETL.py"), commented.encode("utf-8"))
        write(os.path.join(tree, "archive", "notes.txt"), base.encode("utf-8"))
        write(os.path.join(tree, "__pycache__", "etl.py"), louder.encode("utf-8"))
        write(os.path.join(tree, "scheduled", "etl_old.py"),
              louder.encode("utf-8"))
        write(os.path.join(tree, "archive", "torn.py"), torn.encode("utf-8"))
        latin = os.path.join(tree, "scheduled", "latin.py")
        write(latin, b"# caf\xe9\nx = 1\n")

        check(decode(b"x = 1\n") == "x = 1\n", "plain ASCII bytes decode")
        check(decode(b"# caf\xc3\xa9\n") == u"# caf\u00e9\n",
              "UTF-8 bytes decode as UTF-8")
        check(decode(b"# caf\xe9\n") == u"# caf\u00e9\n",
              "bytes that are not UTF-8 fall back to latin-1"
              "  <-- pinned defect")

        found = walk_sources(tree, [".py"])
        check(len(found) == 5, "the walk reads .py and leaves .txt alone")
        check(not any("__pycache__" in p for p in found),
              "the walk never descends into __pycache__  <-- pinned defect")
        check(walk_sources(latin, [".py"]) == [latin],
              "a single file path scans exactly that file")
        check(walk_sources(tree, [".txt"])
              == [os.path.join(tree, "archive", "notes.txt")],
              "--ext .txt reads the text file instead")

        loaded = load(os.path.join(tree, "scheduled", "etl.py"))
        check(loaded.name == "etl.py",
              "a loaded file is named by its basename, lower cased")
        check(loaded.readable, "a loaded file is tokenized")
        check(load(latin).readable, "a latin-1 file is loaded and read")

        # Written inside __pycache__ on purpose: the walk never descends
        # there, so these two prove the identity rule without disturbing any
        # count asserted above. One comment, two encodings of it. The bytes
        # differ, the decoded text does not, and the 381-distinct-contents
        # figure this tool reports depends on which of the two it hashes.
        twin_a = os.path.join(tree, "__pycache__", "twin_utf8.py")
        twin_b = os.path.join(tree, "__pycache__", "twin_latin.py")
        write(twin_a, b"x = 1\n# caf\xc3\xa9\n")
        write(twin_b, b"x = 1\n# caf\xe9\n")
        check(decode(b"x = 1\n# caf\xc3\xa9\n")
              == decode(b"x = 1\n# caf\xe9\n"),
              "two encodings of one comment decode to the same text")
        check(load(twin_a).digest != load(twin_b).digest,
              "identity is the raw bytes, so those are two contents"
              "  <-- pinned defect")
        check(load(twin_a).version == load(twin_b).version,
              "but one version, because the only difference is a comment")

        code, text = run_main([tree])
        check(code == 1, "a destructive disagreement exits 1")
        check("DESTRUCTIVE" in text, "and the run says so")
        check("scanned 5 file(s)" in text, "the run counts the files it read")
        check("could not be tokenized" in text,
              "the run names how many files it could not read")
        code, text = run_main([tree, "--min-class", "cosmetic",
                               "--fail-on", "cosmetic", "--near", "0.5",
                               "--show-diff"])
        check(code == 1, "every reporting flag together still exits 1")
        check("drifted across a rename" in text,
              "--near reports the renamed copy  <-- pinned defect")
        code, text = run_main([os.path.join(tree, "scheduled")])
        check(code == 0, "a folder with no repeated name exits 0")
        check("CLEAN" in text, "and the run says CLEAN")
        check("no group at or above" in text,
              "and explains that nothing met --min-class")

        report_path = os.path.join(tree, "drift.json")
        code, text = run_main([tree, "--out", report_path])
        check(not os.path.exists(report_path),
              "--out without --apply writes nothing at all  <-- pinned defect")
        check("not written" in text, "and says the file was not written")
        code, text = run_main([tree, "--out", report_path, "--apply"])
        check(os.path.exists(report_path), "--apply writes the JSON report")
        handle = open(report_path)
        try:
            saved = json.load(handle)
        finally:
            handle.close()
        check(saved["counts"]["files"] == 5,
              "the written report holds the counts")

        # A file over the read limit must be named, not dropped. The limit is
        # lowered rather than writing 16 MB of fixture to prove it.
        keep_limit = READ_LIMIT
        READ_LIMIT = 20
        try:
            raises(lambda: load(os.path.join(tree, "scheduled", "etl.py")),
                   "a file over the read limit raises Unreadable")
            code, text = run_main([tree])
            check("skipped" in text,
                  "an oversized file is named as skipped, never dropped"
                  "  <-- pinned defect")
            check("scanned 2 file(s)" in text,
                  "the files under the limit are still scanned")
        finally:
            READ_LIMIT = keep_limit

        code, _ = run_main([os.path.join(tree, "nowhere")])
        check(code == 2, "a path that does not exist exits 2")
        code, _ = run_main([tree, "--ext", ".zzz"])
        check(code == 2, "a scan that reads nothing exits 2")
        code, _ = run_main([])
        check(code == 64, "no path at all is a usage error")
        code, _ = run_main([tree, "--near", "1.5"])
        check(code == 64, "a --near outside 0 to 1 is a usage error")

        # ---- --parity on real files. Fixtures are synthetic.
        for sub in ("copy_a", "copy_b"):
            os.mkdir(os.path.join(tree, sub))

        def at(sub, name):
            return os.path.join(tree, sub, name)

        write(at("copy_a", "same.txt"), b"one\n")
        write(at("copy_b", "same.txt"), b"one\n")
        write(at("copy_a", "data.bin"), b"abcd")
        write(at("copy_b", "data.bin"), b"abce")
        big = at("copy_a", "big.bin")
        write(big, b"x" * ((1 << 20) + 5))

        size, sums = measure(at("copy_a", "same.txt"), ["md5"])
        check(size == 4 and sums["sha256"] == hashlib.sha256(b"one\n").hexdigest()
              and sums["md5"] == hashlib.md5(b"one\n").hexdigest(),
              "measure returns the size, the sha256 and any digest asked for")
        check(measure(big)[0] == (1 << 20) + 5,
              "a file larger than one read chunk is measured whole")
        raises(lambda: measure(at("copy_a", "nowhere.txt")),
               "measuring a file that is not there raises OSError", OSError)

        def table(rows, head="| Copies | Identical? |\n|---|---|\n"):
            body = "".join("| %s | %s |\n"
                           % (" | ".join("`%s`" % p for p in paths), claim)
                           for paths, claim in rows)
            path = os.path.join(tree, "parity.md")
            write(path, (head + body).encode("utf-8"))
            return path

        foreign_root = "/srv/share" if os.name == "nt" else "C:\\share"
        foreign = foreign_root + ("/" if os.name == "nt" else "\\") + "same.txt"
        damaged_path = "C:\x07rcgisserver\\same.txt"
        a_same, b_same = at("copy_a", "same.txt"), at("copy_b", "same.txt")
        bad_table = table([
            ((a_same, b_same), "identical"),
            ((at("copy_a", "data.bin"), at("copy_b", "data.bin")),
             "size-identical"),
            ((a_same, at("copy_c", "same.txt")), "identical"),
            ((damaged_path, a_same), "identical"),
            ((foreign, a_same), "identical"),
            ((at("copy_a", "data.bin"), at("copy_b", "data.bin")),
             "**NO** - diverged by design"),
        ])
        before = sorted((d, tuple(sorted(f))) for d, _, f in os.walk(tree))
        code, text = run_main(["--parity", bad_table])
        check(code == 1, "a table whose claim the bytes contradict exits 1")
        for word in ("FALSE", "MISSING", "CORRUPT", "UNVERIFIABLE"):
            check(word + " " in text or word + "  " in text,
                  "the run reports a %s row" % word.lower())
        check("held=1" in text and "false=1" in text,
              "the run counts one held row and one false row")
        check("same size (4 B), different bytes" in text,
              "a size-only claim is shown to be size-only  <-- pinned defect")
        check("1 say the copies differ" in text,
              "a row that says the copies differ is counted, not judged")
        check("\x07" not in text and all(ord(c) < 128 for c in text),
              "the report never echoes a raw control byte and is pure ASCII"
              "  <-- pinned defect")
        check("read as 'C:\\\\arcgisserver" in text,
              "a corrupt path is shown as the escape it was before it broke")
        check("foreign" in text and "unverifiable" in text,
              "a foreign path is named foreign and left unverified")
        check("FAIL:" in text, "and the run says FAIL")
        check(not any(line.startswith("HELD") for line in text.splitlines()),
              "a row that holds is counted but not listed")
        after = sorted((d, tuple(sorted(f))) for d, _, f in os.walk(tree))
        check(before == after,
              "a parity run writes nothing under the tree  <-- pinned defect")

        out_path = os.path.join(tree, "parity.json")
        code, text = run_main(["--parity", bad_table, "--out", out_path])
        check(not os.path.exists(out_path) and "not written" in text,
              "--parity --out without --apply writes nothing  <-- pinned defect")
        code, text = run_main(["--parity", bad_table, "--out", out_path,
                               "--apply"])
        handle = open(out_path)
        try:
            raw = handle.read()
        finally:
            handle.close()
        saved = json.loads(raw)
        check(saved["counts"]["false"] == 1 and saved["rows"][0]["verdict"] == "false",
              "--apply writes the parity report, worst row first")
        check("\\u0007" in raw and "\x07" not in raw,
              "the report holds the BEL as a JSON escape, not a raw byte")
        check(any(p["class"] == "corrupt"
                  for row in saved["rows"] for p in row["paths"]),
              "the report carries the class of each path")

        ok_table = table([((a_same, b_same), "identical")])
        code, text = run_main(["--parity", ok_table])
        check(code == 0 and "CLEAN" in text and "held=1" in text,
              "a table whose claims hold exits 0")

        part_table = table([((a_same, b_same, foreign), "identical")])
        code, text = run_main(["--parity", part_table])
        check(code == 0 and "PARTIAL" in text,
              "two matching copies and a foreign one is partial and exits 0")
        code, text = run_main(["--parity", part_table, "--strict"])
        check(code == 1, "--strict exits 1 on a partial row")

        foreign_table = table([((foreign, a_same), "identical")])
        code, text = run_main(["--parity", foreign_table])
        check("unverifiable=1" in text and code == 0,
              "a foreign path is unverifiable, not missing, and exits 0")
        code, text = run_main(["--parity", foreign_table, "--strict"])
        check(code == 1, "--strict exits 1 on an unverifiable row")
        code, text = run_main(["--parity", foreign_table, "--map",
                               "%s=%s" % (foreign_root,
                                          os.path.join(tree, "copy_b"))])
        check("held=1" in text and code == 0,
              "--map makes the foreign path checkable  <-- pinned defect")

        rel_table = table([(("copy_a/same.txt", "copy_b/same.txt"),
                            "identical")])
        code, text = run_main(["--parity", rel_table])
        check("unverifiable=1" in text, "relative paths are unchecked without --base")
        code, text = run_main(["--parity", rel_table, "--base", tree])
        check("held=1" in text, "--base makes relative paths checkable")

        stale_path = os.path.join(tree, "stale.md")
        write(stale_path, ("| Copies | Identical? |\n|---|---|\n"
                           "| `%s` (99 B) | `%s` | identical |\n"
                           % (a_same, b_same)).encode("utf-8"))
        code, text = run_main(["--parity", stale_path])
        check(code == 1 and "STALE" in text and "table says 99 B" in text,
              "a stated size the file no longer has exits 1")

        shape_path = os.path.join(tree, "shape.md")
        write(shape_path, (
            "| File | `%s` | `%s` | Identical? |\n|---|---|---|---|\n"
            "| same.txt | 4 B | 4 B | size-identical |\n"
            "| data.bin | 4 B | 4 B | size-identical |\n"
            % (os.path.join(tree, "copy_a"), os.path.join(tree, "copy_b"))
        ).encode("utf-8"))
        code, text = run_main(["--parity", shape_path])
        check(code == 1 and "held=1" in text and "false=1" in text,
              "a table with one folder per column is joined and checked")

        empty_path = os.path.join(tree, "empty.md")
        write(empty_path, b"| a | b |\n|---|---|\n| x | y |\n")
        code, _ = run_main(["--parity", empty_path])
        check(code == 2, "a table with no claim of identical exits 2")
        code, _ = run_main(["--parity", os.path.join(tree, "nowhere.md")])
        check(code == 2, "a table that does not exist exits 2")
        code, _ = run_main(["--parity", ok_table, "--base", a_same])
        check(code == 2, "a --base that is not a folder exits 2")
        keep_limit = READ_LIMIT
        READ_LIMIT = 5
        try:
            code, _ = run_main(["--parity", ok_table])
        finally:
            READ_LIMIT = keep_limit
        check(code == 2, "a table over the read limit exits 2")
        code, _ = run_main(["--parity", ok_table, tree])
        check(code == 64, "a path and --parity together is a usage error")
        code, _ = run_main([tree, "--strict"])
        check(code == 64, "--strict without --parity is a usage error")
        code, _ = run_main([tree, "--base", tree])
        check(code == 64, "--base without --parity is a usage error")
        code, _ = run_main([tree, "--map", "a=b"])
        check(code == 64, "--map without --parity is a usage error")
    finally:
        shutil.rmtree(tree)

    # ---- argument handling
    args = _parse(["tree"])
    check(args.path == "tree", "the path is positional")
    check(args.apply is False, "--apply defaults to OFF")
    check(args.out is None, "--out defaults to nothing")
    check(args.min_class == CODE, "--min-class defaults to code")
    check(args.fail_on == DESTRUCTIVE, "--fail-on defaults to destructive")
    check(args.near is None, "--near defaults to OFF")
    check(args.show_diff is False, "--show-diff defaults to OFF")
    check(args.ext == list(SOURCE_EXTENSIONS),
          "--ext defaults to the configured source list")
    check(args.parity is None and args.base is None and args.map is None
          and args.strict is False,
          "--parity, --base, --map and --strict default to OFF")
    check(_parse(["--parity", "t.md"]).parity == "t.md", "--parity is read")
    check(_parse(["--parity", "t.md", "--map", "C:\\w=/mnt/w", "--map",
                  "D:\\x=/mnt/x"]).map == [("C:\\w", "/mnt/w"), ("D:\\x", "/mnt/x")],
          "--map is read, and repeats")
    raises(lambda: _parse(["--parity", "t.md", "--map", "nonsense"]),
           "a --map with no equals sign is rejected", SystemExit)
    raises(lambda: _parse(["--parity", "t.md", "--map", "=folder"]),
           "a --map with no prefix is rejected", SystemExit)
    raises(lambda: _parse(["--par", "t.md"]),
           "a unique prefix of --parity is refused by the parser"
           "  <-- pinned defect", SystemExit)
    raises(lambda: _parse(["--parity", "t.md", "--app"]),
           "a shorter prefix of --apply is refused by the parser"
           "  <-- pinned defect", SystemExit)
    raises(lambda: _parse(["--parity", "t.md", "--str"]),
           "a prefix of --strict is refused by the parser", SystemExit)
    check(_parse(["tree", "--min-class", "cosmetic"]).min_class == COSMETIC,
          "--min-class is read")
    check(_parse(["tree", "--fail-on", "path"]).fail_on == PATH,
          "--fail-on is read")
    check(_parse(["tree", "--near", "0.8"]).near == 0.8, "--near is read")
    check(_parse(["tree", "--ext", ".py,.pyw,.txt"]).ext
          == [".py", ".pyw", ".txt"], "--ext splits on commas")
    check(_parse(["tree", "--out", "r.json", "--apply"]).apply is True,
          "--apply is read")
    check(_parse(["--self-test"]).self_test, "--self-test parses with no path")
    raises(lambda: _parse(["tree", "--min-class", "scary"]),
           "an unknown --min-class is rejected", SystemExit)
    raises(lambda: _parse(["tree", "--ap"]),
           "a unique prefix of --apply is refused by the parser"
           "  <-- pinned defect", SystemExit)

    return finish()


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="clonedrift.py",
        allow_abbrev=False,
        description="Group scripts that share a name, and rank what the "
                    "copies disagree about.",
        epilog="Nothing is written without --apply.",
    )
    ap.add_argument("path", nargs="?", help="folder or file to scan")
    ap.add_argument("--ext", default=",".join(SOURCE_EXTENSIONS),
                    type=lambda s: [e.strip()
                                    for e in s.split(",") if e.strip()],
                    help="comma separated extensions to read (default %s)"
                         % ",".join(SOURCE_EXTENSIONS))
    ap.add_argument("--min-class", dest="min_class", default=CODE,
                    choices=list(CLASSES),
                    help="lowest class to report (default code)")
    ap.add_argument("--fail-on", dest="fail_on", default=DESTRUCTIVE,
                    choices=list(CLASSES),
                    help="lowest class that exits 1 (default destructive)")
    ap.add_argument("--near", type=float, default=None,
                    help="also report copies that drifted across a rename, "
                         "at or above this similarity from 0 to 1")
    ap.add_argument("--show-diff", dest="show_diff", action="store_true",
                    help="print the diff between the first two versions")
    ap.add_argument("--out", default=None, help="path for a JSON report")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--parity", default=None, metavar="TABLE",
                    help="check a Markdown parity table: every row that says "
                         "its copies are identical is compared byte for byte")
    ap.add_argument("--base", default=None,
                    help="with --parity: folder that relative paths in the "
                         "table are read against")
    ap.add_argument("--map", action="append", default=None,
                    type=_map_arg, metavar="PREFIX=FOLDER",
                    help="with --parity: read paths under PREFIX from FOLDER "
                         "on this host. Repeatable.")
    ap.add_argument("--strict", action="store_true",
                    help="with --parity: also fail on rows that could only "
                         "be partly checked or not at all")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _map_arg(text):
    prefix, _, folder = text.partition("=")
    if not prefix or not folder:
        raise argparse.ArgumentTypeError("expected PREFIX=FOLDER, got %r"
                                         % text)
    return prefix, folder


def run_parity(args):
    """The --parity mode. Reads a table and the files it names, writes nothing."""
    if args.path:
        print("error: pass a path to scan or --parity, not both.",
              file=sys.stderr)
        return 64
    if not os.path.isfile(args.parity):
        print("error: no such table: %s" % args.parity, file=sys.stderr)
        return 2
    if args.base is not None and not os.path.isdir(args.base):
        print("error: --base is not a folder: %s" % args.base,
              file=sys.stderr)
        return 2
    try:
        text = decode(read_bytes(args.parity))
    except Unreadable as exc:
        print("error: %s: %s" % (args.parity, exc), file=sys.stderr)
        return 2

    host = "nt" if os.name == "nt" else "posix"
    entries, skipped = parse_table(text)
    if not entries:
        print("error: no row in %s claims that copies are identical."
              % args.parity, file=sys.stderr)
        return 2
    findings = [judge(e, host, args.base, args.map or [], measure)
                for e in entries]
    counts = parity_counts(findings)

    print("parity table %s: %d row(s) claim identical copies"
          % (args.parity, len(entries)))
    print("  " + "  ".join("%s=%d" % (v, counts[v]) for v in VERDICTS))
    print("%d row(s) not judged: %d say the copies differ, %d say both, "
          "%d say nothing, %d name fewer than two paths"
          % (sum(skipped.values()), skipped[DIFFERS], skipped[MIXED],
             skipped[NO_CLAIM], skipped[UNPAIRED]))
    print("")
    for finding in sorted(findings, key=lambda f: (f.rank, f.line)):
        if finding.verdict != V_HELD:
            for line in describe_finding(finding):
                print(line)
            print("")

    if args.out:
        if args.apply:
            handle = open(args.out, "w")
            try:
                json.dump(as_parity_report(args.parity, host, skipped,
                                           findings),
                          handle, indent=2, sort_keys=True)
            finally:
                handle.close()
            print("wrote %s" % args.out)
        else:
            print("Check only. %s was not written. Re-run with --apply."
                  % args.out)

    bad = parity_failing(findings, args.strict)
    if bad:
        print("FAIL: %d row(s) claim identical copies that the files do not "
              "bear out." % len(bad))
        return 1
    print("CLEAN: no row's claim is contradicted.")
    return 0


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if args.parity is not None:
        return run_parity(args)
    if args.base is not None or args.map or args.strict:
        print("error: --base, --map and --strict only apply with --parity.",
              file=sys.stderr)
        return 64
    if not args.path:
        print("error: a path to scan is required. Use --self-test to verify "
              "the tool without a share.", file=sys.stderr)
        return 64
    if args.near is not None and not 0.0 <= args.near <= 1.0:
        print("error: --near must be between 0 and 1.", file=sys.stderr)
        return 64
    if not os.path.exists(args.path):
        print("error: no such path: %s" % args.path, file=sys.stderr)
        return 2

    sources = []
    skipped = []
    for path in walk_sources(args.path, args.ext):
        try:
            sources.append(load(path))
        except (Unreadable, IOError, OSError) as exc:
            skipped.append((path, str(exc)))

    if not sources:
        print("error: nothing was scanned under %s" % args.path,
              file=sys.stderr)
        return 2

    groups = analyse(sources)
    counts = tally(sources, groups)
    pairs = near_pairs(sources, args.near) if args.near is not None else []

    print("scanned %d file(s), %d distinct by content, %d name(s)"
          % (counts["files"], counts["distinct"], counts["names"]))
    print("%d file(s) sit in an identical group, largest %d"
          % (counts["copied_files"], counts["largest_identical_group"]))
    print("%d name(s) carry two or more different contents, across %d file(s)"
          % (counts["divergent_names"], counts["divergent_files"]))
    print("  " + "  ".join("%s=%d" % (c, counts["by_class"][c])
                           for c in reversed(CLASSES)))
    if counts["unreadable"]:
        print("%d file(s) could not be tokenized and were never called clean"
              % counts["unreadable"])
    for path, why in skipped:
        print("  skipped %s (%s)" % (path, why))
    print("")

    shown = [g for g in groups if g.rank >= CLASS_RANK[args.min_class]]
    if shown:
        for group in shown:
            for line in describe(group, args.show_diff):
                print(line)
            print("")
    else:
        print("no group at or above --min-class %s\n" % args.min_class)

    if pairs:
        print("copies that drifted across a rename, at or above %.2f:"
              % args.near)
        for score, left, right in pairs:
            print("  %.2f  %s" % (score, left.path))
            print("        %s" % right.path)
        print("")

    if args.out:
        if args.apply:
            handle = open(args.out, "w")
            try:
                json.dump(as_report(counts, groups, pairs), handle, indent=2,
                          sort_keys=True)
            finally:
                handle.close()
            print("wrote %s" % args.out)
        else:
            print("Check only. %s was not written. Re-run with --apply."
                  % args.out)

    bad = failing(groups, args.fail_on)
    if bad:
        print("FAIL: %d name(s) at or above --fail-on %s."
              % (len(bad), args.fail_on))
        return 1
    print("CLEAN: no name at or above --fail-on %s." % args.fail_on)
    return 0


if __name__ == "__main__":  # pragma: no cover - the self-test calls main()
    sys.exit(main())
