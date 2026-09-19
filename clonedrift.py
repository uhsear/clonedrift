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
    python clonedrift.py --self-test

Exit codes: 0 clean, 1 drift at or above --fail-on, 2 nothing could be scanned,
64 usage error.
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


def load(path):
    """Read one file into a Source. Hashes the bytes, not the decoded text."""
    handle = open(path, "rb")
    try:
        data = handle.read(READ_LIMIT + 1)
    finally:
        handle.close()
    if len(data) > READ_LIMIT:
        raise Unreadable("larger than the %d byte read limit" % READ_LIMIT)
    return Source(os.path.basename(path), path, decode(data),
                  content=hashlib.md5(data).hexdigest())


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

def self_test():
    """Assertions over the decision core. No share, no network, no arcpy."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=Unreadable):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

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

    print("-" * 70)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for item in failed:
            print("  FAILED: %s" % item)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="clonedrift.py",
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
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

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


if __name__ == "__main__":
    sys.exit(main())
