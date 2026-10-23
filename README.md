# clonedrift

Name every script on a share that exists in more than one version, and say what the versions
disagree about. Exits non-zero when two copies of one script disagree about a call that destroys
data. With `--parity`, it also reads a Markdown parity table and opens the copies, to catch a table
that says "identical" after the copies have diverged.

A nightly job pulls a parcel roll off a vendor host, empties the production feature class and
appends what it got. Somebody finds the bug, fixes it, tests it, and goes home. Nothing changes at
2am, because Task Scheduler runs a different copy.

On one real share, this tool found fifteen copies of that job under a single name, holding four
versions. Two of those versions differ by one edit and one stray space. One runs `os.remove` and
`shutil.rmtree` on the staging folder when the load finishes. The other has those two lines, and
the message that announces them, commented out. Run the wrong copy and the extract is gone.

A byte-for-byte deduplicator sees none of this, because the copies are not identical. Nobody on
the team can say from memory which copy the scheduler runs. Opened on its own, every copy looks
correct.

There is a second way to lose that argument. The team keeps a parity table: a Markdown file that
lists each script, its deployed copy and its backup, and a column that says "identical". The
column was filled in by comparing file sizes. Later somebody changes one letter of the load
script, which keeps its length, and the table goes on saying yes. In the same table, the Windows
paths were pasted through a Python string, where `\a` is the BEL character, so `C:\arcgis` became
`C:` plus a control byte plus `rcgis`. That path never named a folder, and nothing flagged it. Each
row of the table was believed because the table had been right once.

`--parity` checks the table instead of trusting it. It also refuses to call a path "missing" when
the honest answer is "I could not look": a Windows path read on Linux is *foreign*, a placeholder
or a relative path with no base is *ambiguous*, and a path with a control byte is *corrupt*.
Without those classes, every row of a table written on another kind of host is a false alarm, and
the report is ignored.

```
$ python clonedrift.py --self-test
clonedrift self-test: no share, no network, no arcpy
----------------------------------------------------------------------
PASS  a copy with an extra comment is the same version  <-- pinned defect
PASS  a copy with a trailing comment is the same version
PASS  a copy with extra blank lines is the same version
...
PASS  a copy naming another host is a second version
...
PASS  a statement boundary is part of the version  <-- pinned defect
PASS  a Python 2 print statement is read, not skipped  <-- pinned defect
PASS  a Python 2 file is fingerprinted like any other
PASS  a Python 2 except clause is read, not skipped
PASS  an unterminated string is unreadable
...
PASS  an ERRORTOKEN holding real text is an error  <-- pinned defect
PASS  an ERRORTOKEN holding only spaces is not  <-- pinned defect
...
PASS  a drive letter literal is a path literal  <-- pinned defect
PASS  an ftp url literal is a path literal, by the same shape
...
PASS  an ArcCatalog connection literal is a path literal  <-- pinned defect
PASS  a geodatabase name on its own is a path literal  <-- pinned defect
...
PASS  a module docstring naming a gdb is not a path literal  <-- pinned defect
PASS  a function docstring naming an sde is not a path literal  <-- pinned defect
PASS  a commented-out path is not a path literal
PASS  the same path in single and double quotes is one literal  <-- pinned defect
...
PASS  rmtree live in one copy and commented in the other is drift  <-- pinned defect
PASS  the evidence says which side has it commented out
PASS  two copies that both comment it out do not disagree
...
PASS  TruncateTable matches despite the _management suffix  <-- pinned defect
...
PASS  a destructive call whose target changed is drift
...
PASS  a host disagreement outranks a print message  <-- pinned defect
...
PASS  a path literal both versions share is not evidence  <-- pinned defect
...
PASS  an unreadable copy never manufactures a path disagreement  <-- pinned defect
PASS  and no literal is offered as evidence against a copy never read  <-- pinned defect
PASS  a lone unreadable copy is never called cosmetic  <-- pinned defect
...
PASS  that group holds two versions, not three  <-- pinned defect
...
PASS  copies whose names differ only in case group together  <-- pinned defect
...
PASS  a group prints its own class
...
PASS  similarity is symmetric  <-- pinned defect
...
PASS  two empty sets score zero, not one  <-- pinned defect
...
PASS  a file is never paired with its own name  <-- pinned defect
...
PASS  three copies of one content report two content pairs, not five  <-- pinned defect
...
PASS  a claim that rests on size alone is still a claim of identical
...
PASS  a span with only a drive colon and a BEL is still a path  <-- pinned defect
...
PASS  a header's folders do not leak into the table after it  <-- pinned defect
...
PASS  a form feed or BEL inside a path does not tear the row  <-- pinned defect
...
PASS  a drive path is foreign on a posix host, not missing  <-- pinned defect
...
PASS  an admin share is not mistaken for a variable  <-- pinned defect
...
PASS  a path with a BEL is corrupt, never a missing file  <-- pinned defect
...
PASS  a drive-relative path is ambiguous  <-- pinned defect
...
PASS  a prefix matches whole folder names only  <-- pinned defect
PASS  an extended-length drive path is a file, not a wildcard  <-- pinned defect
...
PASS  \\.\PhysicalDrive0 is a device, never opened  <-- pinned defect
...
PASS  two leading slashes are a UNC share on Windows, ambiguous on posix  <-- pinned defect
...
PASS  same size and different bytes is false  <-- pinned defect
...
PASS  one local copy and one foreign is unverifiable, not missing  <-- pinned defect
...
PASS  one file named twice is not two copies  <-- pinned defect
...
PASS  a table in UTF-8, UTF-8 with a BOM or UTF-16 is read as text  <-- pinned defect
...
PASS  bytes that are not UTF-8 fall back to latin-1  <-- pinned defect
...
PASS  the walk never descends into __pycache__  <-- pinned defect
...
PASS  identity is the raw bytes, so those are two contents  <-- pinned defect
PASS  but one version, because the only difference is a comment
...
PASS  --near reports the renamed copy  <-- pinned defect
...
PASS  --out without --apply writes nothing at all  <-- pinned defect
...
PASS  an oversized file is named as skipped, never dropped  <-- pinned defect
...
PASS  a size-only claim is shown to be size-only  <-- pinned defect
...
PASS  the report never echoes a raw control byte and is pure ASCII  <-- pinned defect
...
PASS  a parity run writes nothing under the tree  <-- pinned defect
...
PASS  --map makes the foreign path checkable  <-- pinned defect
...
PASS  a table saved with a UTF-8 BOM is read like any other  <-- pinned defect
PASS  a table saved with UTF-16 is read like any other  <-- pinned defect
PASS  a row naming one file twice is not held  <-- pinned defect
...
PASS  a unique prefix of --parity is refused by the parser  <-- pinned defect
PASS  a shorter prefix of --apply is refused by the parser  <-- pinned defect
...
PASS  an unknown --min-class is rejected
PASS  a unique prefix of --apply is refused by the parser  <-- pinned defect
----------------------------------------------------------------------
339 assertions, 0 failed
```

## Requirements

Python 3.9 or later. Standard library only: `argparse`, `difflib`, `hashlib`, `io`, `json`, `os`,
`re`, `stat`, `sys` and `tokenize`, with `shutil` and `tempfile` for the self-test's own temporary
tree.
No `arcpy`, no `git`, no network and no credentials. It runs the same on ArcGIS Pro's Python and
on a plain `python3`.

```
git clone https://github.com/uhsear/clonedrift.git
python clonedrift.py --self-test
```

The self-test was run on Windows with Python 3.13, 3.12, 3.10 and 3.9, and on Linux with Python
3.12. All five report 339 assertions and 0 failed. The count is equal on every host because no
assertion branches on the operating system: the path-class tests pass the host in as an argument,
and the filesystem tests build their paths with `os.path.join`.

Branch coverage of `clonedrift.py` is 100 percent (1274 statements, 312 branches), measured on
Windows with `coverage run --branch clonedrift.py --self-test`. One line is excluded by a
`# pragma: no cover` comment: the `if __name__ == "__main__"` guard, because the self-test calls
`main()` directly and never starts the file as a script. `coverage` is not installed on the Linux
host, so coverage was not measured there. `pyflakes` is clean, and `python -W error` compiles the
file on Windows and on Linux.

## Usage

The path can be a folder or a single file.

```
python clonedrift.py /path/to/share
python clonedrift.py /path/to/share --show-diff
python clonedrift.py /path/to/share --min-class cosmetic --fail-on path
python clonedrift.py /path/to/share --near 0.8
python clonedrift.py /path/to/share --out drift.json --apply
python clonedrift.py --parity PARITY.md --base /path/to/copies
python clonedrift.py --parity PARITY.md --map "C:\work=/mnt/work" --strict
```

The scan is read-only. The JSON report is written only with `--apply`.

A real run against a twelve-file test share. The share holds three copies of one job, two copies
of a download script that name different hosts, and a renamed copy of the job.

```
$ python clonedrift.py share --min-class cosmetic --near 0.6
scanned 12 file(s), 11 distinct by content, 6 name(s)
2 file(s) sit in an identical group, largest 2
5 name(s) carry two or more different contents, across 11 file(s)
  destructive=1  path=1  code=2  cosmetic=1
2 file(s) could not be tokenized and were never called clean

DESTRUCTIVE roadsync.py  (3 copies, 2 versions)  copies disagree about a call that destroys data
    version: share\archive\roadsync.py
    version: share\desktop-copy\roadsync.py
      - shutil.rmtree(STAGE)
      + # shutil.rmtree(STAGE) [commented out]

PATH        parcelpull.py  (2 copies, 2 versions)  copies name different hosts or paths
    version: share\desktop-copy\parcelpull.py
    version: share\scheduled\parcelpull.py
      only in some versions: ftp.example.net
      only in some versions: ftp.example.org

CODE        fieldcalc.py  (2 copies, 2 versions)  copies disagree about code
    version: share\archive\fieldcalc.py
    version: share\scheduled\fieldcalc.py

CODE        notes.py  (2 copies, 2 versions)  copies disagree about code
    version: share\archive\notes.py
    version: share\desktop-copy\notes.py
    UNREADABLE: share\archive\notes.py (TokenError: ('unterminated string literal (detected at line 1)', (1, 8)))
    UNREADABLE: share\desktop-copy\notes.py (TokenError: ('unterminated string literal (detected at line 1)', (1, 8)))

COSMETIC    zoning.py  (2 copies, 1 version)  copies differ only in comments or whitespace
    version: share\archive\Zoning.py

copies that drifted across a rename, at or above 0.60:
  0.78  share\archive\roadsync.py
        share\archive\roadsync_old.py
  0.61  share\archive\roadsync_old.py
        share\desktop-copy\roadsync.py

FAIL: 1 name(s) at or above --fail-on destructive.
```

Every name, host and path in that example is invented for this README.

| Flag | Default | What it does |
|---|---|---|
| `path` | none | Folder or file to scan. Required unless `--self-test` is passed. |
| `--ext` | `.py,.pyw` | Comma separated extensions to read. |
| `--min-class` | `code` | Lowest class to print: `cosmetic`, `code`, `path` or `destructive`. |
| `--fail-on` | `destructive` | Lowest class that exits 1. |
| `--near` | off | Also report copies that drifted across a rename, at or above this score. |
| `--show-diff` | off | Print the diff between the first two versions of each group. |
| `--out` | none | Path for a JSON report. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--parity` | none | Markdown parity table to check. Replaces `path`. |
| `--base` | none | With `--parity`: folder that relative paths in the table are read against. |
| `--map` | none | With `--parity`: `PREFIX=FOLDER`, read paths under `PREFIX` from `FOLDER`. Repeatable. |
| `--strict` | off | With `--parity`: also exit 1 on rows that were only partly checked, or not at all. |
| `--self-test` | off | Run the offline assertions and exit. |

Every parser option is spelled out in full: the parser is built with `allow_abbrev=False`, so
`--ap` for `--apply`, `--app`, `--par` for `--parity` and `--str` for `--strict` are all refused. A
mistyped prefix cannot turn a check into a write.

## Checking a parity table

The input is a Markdown table. Two shapes are read.

```
| Script  | Copy A                 | Copy B            | Identical? |
|---------|------------------------|-------------------|------------|
| rollup  | `C:\shared\rollup.py`  | `deployed/rollup.py` | **YES** |

| File       | `deployed/` | `backup/` | Identical?     |
|------------|-------------|-----------|----------------|
| notes.md   | 35 B        | 35 B      | size-identical |
```

In the first, each row names its copies in backticks. In the second, the header names one folder
per column, and each row's first cell is a file name that is joined to each folder. A row is
checked when its words claim the copies are the same (`identical`, `in parity`, `byte-for-byte`,
`same`, `match`, `yes`) and say nothing against it. A row that says they differ, or says both, is
counted and left alone. A byte count (`33,782 B`) or a hex digest of 32, 40 or 64 digits that
sits in the same cell as a path is checked against the file too.

A real run, on Linux with Python 3.12, over a synthetic share. The table names eight rows that claim
identical copies. The BEL byte in the fourth is really in the file. The table is saved as UTF-8,
and the same table saved as UTF-16 (what Windows PowerShell 5.1 writes for `>`) gives the same counts.

```
$ python3 clonedrift.py --parity PARITY.md --base .
parity table PARITY.md: 8 row(s) claim identical copies
  false=1  missing=1  corrupt=1  stale=0  unverifiable=4  partial=0  held=1
0 row(s) not judged: 0 say the copies differ, 0 say both, 0 say nothing, 0 name fewer than two paths

FALSE        line 3  the table says identical, the bytes differ
    checked  'deployed/loader.sql'  27 B  sha256 4cf350f5ad1b
    checked  'backup/loader.sql'  27 B  sha256 ed15b96c5ab1
    same size (27 B), different bytes: a size-only check calls these identical

MISSING      line 9  a copy the table names is missing or cannot be read
    checked  'deployed/rollup.py'  14 B  sha256 b83a6fbe7ee4
    not a regular file (folder, device or pipe): '/dev/zero'

CORRUPT      line 6  a path holds a control byte, so it never named a file
    checked  'deployed/nightly.py'  11 B  sha256 e408ba48bfb9
    corrupt 'C:\x07rcgis\\nightly.py': control byte 0x07 at offset 2, read as 'C:\\arcgis\\nightly.py'

UNVERIFIABLE line 4  fewer than two copies could be checked on this host
    checked  'deployed/rollup.py'  14 B  sha256 b83a6fbe7ee4
    foreign 'C:\\shared\\gis\\rollup.py': a Windows path on a posix host; add --map

UNVERIFIABLE line 5  fewer than two copies could be checked on this host
    checked  'backup/rollup.py'  14 B  sha256 b83a6fbe7ee4
    ambiguous '/data/<HOST>/mount.sh': placeholder, variable or wildcard

UNVERIFIABLE line 7  fewer than two copies could be checked on this host
    checked  'backup/rollup.py'  14 B  sha256 b83a6fbe7ee4
    foreign '\\\\?\\C:\\shared\\gis\\rollup.py': a Windows path on a posix host; add --map

UNVERIFIABLE line 8  fewer than two copies could be checked on this host
    checked  'deployed/rollup.py'  14 B  sha256 b83a6fbe7ee4
    './deployed/rollup.py' is the same file as 'deployed/rollup.py': one file is not a copy of itself

FAIL: 3 row(s) claim identical copies that the files do not bear out.
```

Paths are printed escaped, so a control byte shows as `\x07` and the report stays plain ASCII. The
share and the table are invented for this README. The four `UNVERIFIABLE` rows exit 0 by default.
`--strict` turns them, and any `PARTIAL` row, into exit 1: the same table gives `FAIL: 7 row(s)`.
`--map "C:\shared\gis=deployed"` makes the first of them checkable, and that row then holds
(`unverifiable=3`, `held=2`).

Two rows in that table show the newest checks. Line 8 names `deployed/rollup.py` twice, once as
`./deployed/rollup.py`. One file is identical to itself, so the row proved nothing and is not
`HELD`. Line 9 names `/dev/zero`. A table can name anything, and only a regular file is opened:
a device never ends and a pipe blocks. The same row on a Windows host reads `/dev/zero` as a foreign
path, because Windows cannot open it.

Run on Windows, the same table reports the `C:\shared\gis\rollup.py` row as `MISSING` instead.
That is correct: a Windows host can open a drive path, and the file is not there. The class
depends on the host, by design. The long form `\\?\C:\shared\gis\rollup.py` on line 7 follows the
same rule: foreign on Linux, a file that is not there on Windows.

### Path classes

| Class | Example | What happens |
|---|---|---|
| local | `C:\gis\a.txt`, `//srv/share/a` and `\\?\C:\gis\a.txt` on Windows, `/srv/a` on Linux, a relative path with `--base` | Opened and hashed, if it is a regular file. |
| foreign | `C:\gis\a.txt`, `\\srv\share\a` or `\\?\C:\gis\a.txt` on Linux, `/srv/a` on Windows, a `https://` url | Not opened. Becomes checkable with `--map`. |
| ambiguous | `<HOST>\a`, `%TEMP%\a`, `$HOME/a`, `*.csv`, `\Users\a`, `~/a`, `C:docs\a`, `//srv/share/a` on Linux, `\\.\PhysicalDrive0`, a relative path with no `--base`, `/srv/a\b` | Not opened. Means a different file depending on state the table does not hold, or is a device and not a file. |
| corrupt | `C:` plus a BEL plus `rcgis` | Not opened. The message names the byte, its offset and the path it was before the escape ate it. |

None of the last three is ever reported as a missing file. "I could not look" is not "it is not
there". A row with fewer than two checkable copies is `UNVERIFIABLE`, and a row where some copies
matched and others could not be checked is `PARTIAL`.

### Verdicts

From worst to best: `FALSE` (bytes differ), `MISSING` (a named copy cannot be opened), `CORRUPT`,
`STALE` (a size or digest stated in the table is not what the file is now), `UNVERIFIABLE`,
`PARTIAL`, `HELD`. A row with several problems takes the worst and lists all of them. Exit 1 on
`FALSE`, `MISSING`, `CORRUPT` or `STALE`; with `--strict`, also on `UNVERIFIABLE` and `PARTIAL`.
Exit 2 if the table is missing, over the read limit or holds no row that claims identical copies,
or if `--base` is not a folder.

A copy that is a folder, a device or a pipe is `MISSING` with the note "not a regular file". It is
never opened. A path named twice in one row counts once, whether it is spelled `a/x` and `a/./x`
or one of the two is a symbolic link to the other. The row then has one copy, and one file is not a
copy of itself. Two hard links to one file are not caught (see Limits).

## What it checks

- **Which copies are one version and which are a fork.** Every file is tokenized and reduced to
  its significant tokens. Comments, blank lines, trailing whitespace and indentation width are
  dropped. A copy that somebody reformatted is therefore the same version, and a copy that
  somebody edited is not. Two copies with different bytes and the same token stream are reported
  as one version.
- **Whether the versions disagree about a destructive call.** The raw lines of every pair of
  versions are diffed. A differing line that calls `rmtree`, `os.remove`, `os.unlink`,
  `Delete_management`, `DeleteFeatures`, `DeleteRows` or `TruncateTable` makes the group
  DESTRUCTIVE. Raw lines are used rather than tokens because the case that matters is a line one
  copy runs and the other has commented out. A token stream has already discarded the commented
  copy. The evidence marks which side is commented.
- **Whether the versions name different hosts or paths.** Each version's string literals are
  collected, filtered to the ones that name a machine or a place on one, and compared. Docstrings
  are excluded. Quote style is ignored, so a reformatted file is not reported as a host change.
  A path disagreement outranks any other code difference. A path decides which machine the job
  talks to.
- **Files it cannot read.** A file the tokenizer refuses is reported as unreadable. It keeps its
  own content hash, so it is never merged with another copy. A group holding one is never called
  cosmetic, because nothing has been proven about it. It is never ranked on a path comparison
  either: a copy with no token stream has no literals, and reporting every literal in its sibling
  as a disagreement would invent a finding rather than measure one. Such a group is CODE.
- **Python 2.** The tokenizer, not `ast`, does the reading. A legacy ArcGIS share is mostly Python
  2, which `ast.parse` rejects outright. On the 743-file share this was built against, `ast.parse`
  rejects 340 files and the tokenizer reads 738.
- **The same verdict on every interpreter.** Python 3.10 and later raise on an unterminated string.
  Python 3.9, which ArcGIS Pro 3.0 to 3.2 ship, returns a token stream with an error token in it
  and no exception. The tool reads that stream and calls the file unreadable either way. The same
  five files on that share are unreadable under Python 3.9 and under Python 3.13.
- **Renames, with `--near`.** Files whose names differ are compared by the Jaccard similarity of
  their five-token shingle sets. This is the case basename grouping cannot see: the copy somebody
  saved as `roadsync_old.py`.

- **Whether a parity table is telling the truth, with `--parity`.** Each row that claims identical
  copies is checked by hashing the files with sha256, not by size or time. Rows whose copies have
  equal size and different bytes are called out as such. See "Checking a parity table".
- **A table saved by Windows PowerShell.** A UTF-8 byte-order mark in front of the header would
  hide it from the parser, and `>` writes UTF-16. The table is read as UTF-8 with or without a mark,
  or as UTF-16 when it opens with a UTF-16 mark.
- **Why a path is not checkable.** Foreign, ambiguous and corrupt are separate classes, each with a
  reason, so a table written on another kind of host does not fill the report with false
  "missing" rows. The class rules rest on these sources:
  - Python: `\a` in a string literal is the ASCII Bell, and `\f`, `\v`, `\t`, `\r` and `\n` are
    single control characters too. An unrecognised escape such as `\g` keeps its backslash, which
    is why only some Windows folder names break
    (https://docs.python.org/3/reference/lexical_analysis.html, "Escape sequences").
  - Windows: file names cannot hold characters 1 to 31 or NUL. A path starting with a single
    backslash is relative to the current drive, and `C:tmp.txt` is relative to the current directory
    on drive C, so neither names one file without outside state. `D:\` and `d:\` are one volume,
    and a UNC name starts with two backslashes
    (https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file and
    https://learn.microsoft.com/en-us/dotnet/standard/io/file-path-formats).
  - POSIX: a file name may hold any byte except NUL and slash, so a backslash in a Linux path is
    part of a name, not a separator, and a Windows path means nothing there
    (https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap03.html, "Filename"). A
    pathname that begins with exactly two slashes may be read in an implementation-defined way,
    and three or more leading slashes are one slash
    (https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap04.html, "Pathname
    Resolution"). So `//srv/share/a` is ambiguous on Linux and `///srv/a` is `/srv/a`.
  - Windows device paths: `\\?\C:\a` and `\\.\C:\a` name the same file as `C:\a`, and
    `\\?\UNC\srv\share\a` is a UNC path. Windows folds `/` to `\` and reads two leading separators
    as UNC, so `//srv/share/a` is a share there. A `\\.\` or `\\?\` name with anything else after it
    (`\\.\COM1`, `\\.\PhysicalDrive0`) reaches a device. `?` after the prefix is therefore not a
    wildcard
    (https://learn.microsoft.com/en-us/dotnet/standard/io/file-path-formats, "DOS device paths"
    and "Canonicalize separators").
  - PowerShell: in Windows PowerShell 5.1, `>` and `Out-File` write UTF-16LE with a byte-order
    mark, and `-Encoding utf8` writes UTF-8 with one
    (https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding).

## Exit codes

0 no group at or above `--fail-on`, 1 at least one group at or above it, 2 the path does not exist
or nothing could be scanned, 64 usage error. A flag value argparse itself rejects, such as a
`--near` that is not a number or an unknown `--min-class`, is argparse's own error and exits 2.
With `--parity`, the codes are those in "Verdicts" above, and passing both a path and `--parity`,
or `--base`, `--map` or `--strict` without `--parity`, is a usage error that exits 64.

## What it measured

One run of this tool over a 743-file legacy ArcGIS automation share reports 381 distinct contents.
512 of those files sit in a group of byte-identical copies, and the largest group holds ten. 48
filenames carry two or more different contents, across 289 files.

Of those 48 names, 9 disagree about a destructive call and 27 name different hosts or paths. 9
disagree about other code. 3 differ only in comments or whitespace. Five files could not be
tokenized. The scan takes about four seconds.

Those numbers are the argument for the tool. More than two thirds of that share is byte-identical
copies. A deduplicator answers none of the questions that matter about the rest.

## Why not jscpd or copydetect

Both are good, both are maintained, and both read Python 2 without complaint. They will tell you
what fraction of the share is duplicated, down to the matching token ranges. That is a real
measurement and it is not the one an operator needs at 2am.

This is not a new clone detector and does not try to be one. It asks a different question:
**which of these fifteen copies has `rmtree` live, and which host does each one name.** Neither
of those tools groups whole files by name, collapses the copies that differ only in formatting,
and ranks the survivors by whether the disagreement touches a destructive call. That ranking is
the product. The clone detection underneath it is deliberately simple.

It is also not a linter. `ruff` and `pylint` read one file at a time and have no concept of a
second copy of it.

For `--parity`, the plain answer to "are these two files the same" already exists and is good:
`sha256sum -c`, `diff -rq`, `fc /b` and Python's `filecmp` all compare bytes, and any of them is
the right tool when you already have the list of pairs. They do not read the pairs out of a table,
they do not notice the claim was made from sizes, and they report a Windows path on a Linux host
as an error. A GitHub search for `docdrift` on 2026-10-23 returned eight public repositories. By
their descriptions alone, one checks the paths, links and repositories cited in Markdown against
the real tree, and one keeps project documentation in step with code through a language model. I
read the descriptions, not the code. Both ask whether the documentation still matches the project.
This asks whether two copies that the table says are the same still hold the same bytes.

## Limits

- Names are compared in lower case, so `Zoning.py` and `zoning.py` are one group. On Linux those
  are two different files, and a share that deliberately holds both will be grouped as one.
- It compares whole files that share a name. Two copies of one function inside two differently
  named scripts are invisible to the grouping. `--near` covers part of that gap, but it is a
  whole-file score as well.
- The destructive call list is a fixed regular expression in the CONFIGURATION block. It covers
  the `shutil`, `os` and `arcpy` calls that empty things. A destructive call reached through a
  variable, a wrapper function or `getattr` is not matched.
- A path disagreement is detected only when the literal is shaped like a path or a URL. The shapes
  it knows are a UNC share, a drive letter, an `http://`, `https://` or `ftp://` URL, a bare
  `ftp.` host, an ArcCatalog `Database Connections` entry, and a `.sde`, `.gdb` or `.mdb` file.
  A bare hostname with no path syntax around it, such as `gisprod01`, is not recognised. That
  group lands in the `code` class instead.
- String literals are compared as written, after the quotes and any prefix are stripped. Escapes
  are not resolved, so `"C:\\gis"` and `r"C:\gis"` name the same folder and are reported as two
  literals.
- Docstring detection is positional: a string that is a statement on its own. A module that
  assigns its documentation to a variable will have that text scanned for paths.
- Renaming a variable changes the token stream, so two copies that differ only by a rename are two
  versions. `--near` will still pair them.
- `--near` compares every readable pair, which is O(n^2). On the 743-file share it adds about
  fourteen seconds to a four-second scan. At 50,000 files it needs a size and basename prefilter
  first, which is marked in the source.
- Every file is held in memory as text, plus its token stream and its shingle set. A few thousand
  scripts are fine. A tree of generated code is not, and files over 16 MB are named as skipped
  rather than read.
- It reports. It never edits, moves or deletes a copy. Choosing which version wins is a decision
  with an owner, and this tool does not have that owner's context.
- A clean result means no two copies of one name disagree. It does not mean the surviving copy is
  correct.
- `--parity` reads Markdown tables only, one row per line. A path that held a `\n` escape became a
  real line feed, which splits the row in two. The first half is then counted under "say nothing"
  and the second half is not a row, so the claim is never judged. A path with a BEL, form feed,
  tab or carriage return in it is kept whole.
- A path is found in a table only inside backticks, or as a whole cell that starts with a drive
  letter, `\\`, or `/`. A bare file name in backticks is treated as a label, not a place.
- A row is judged by its words. `identical`, `in parity`, `byte-for-byte`, `same`, `match` and
  `yes` claim sameness, and `not`, `differ`, `diverged`, `stale`, `mismatch`, `split` and `no`
  deny it. A cell that says "not identical" contains both and is skipped as mixed. A table written
  in another language will find no claim and exit 2.
- A byte count or digest is attached to a path only when it is in the same cell, or, in the
  folder-per-column shape, in that folder's column. A size in a separate column of a row-per-copy
  table is ignored.
- The class of a path depends on the host that runs the tool. The same table can report a row as
  `UNVERIFIABLE` on Linux and `MISSING` on Windows. `--map` prefixes match whole folder names and
  ignore case, which is right for Windows prefixes and loose for POSIX ones.
- Modification time is not read. A table older than the files it describes is not flagged on its
  own; the byte comparison is what catches it.
- A control byte other than the seven with a backslash escape is shown as `\xNN`, and the "read
  as" hint cannot tell which original text produced it.
- Two names for one file are matched after `os.path.realpath`, which folds dot segments and
  symbolic links, and `os.path.normcase`, which folds case only on a host that does. A hard link is
  not seen, so two hard links to one file look like two copies and the row is judged as a pair that
  matches itself. The symbolic link case was exercised by hand on Linux, not in the self-test,
  because Windows needs a privilege to make one and the assertion count would then differ.
- A relative path under `--base` whose last part is a legacy Windows device name (`CON`, `NUL`,
  `COM1`) is not screened. Before Windows 11 such a name may open the device (Microsoft Learn, "Handle
  legacy devices"). It was not exercised, because no host here runs a pre-Windows 11 build.
- A table in UTF-16 without a byte-order mark is not detected, and is read as text full of NUL
  bytes, so the run exits 2 with "no row ... claims that copies are identical".
- The non-regular-file check was exercised on a folder in the self-test and on `/dev/zero` in the
  Linux run shown above. A named pipe was not tried.
- Not exercised: a real 16 MB table (the test lowers the limit instead). No code path in this tool
  opens a socket or reads a credential, so there is no stub server to run and nothing to grep for.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [litswap](https://github.com/uhsear/litswap) - rewrites the literal this tool tells you to change
- [stalehost](https://github.com/uhsear/stalehost) - finds a host you already know the name of
- [safe-republish](https://github.com/uhsear/safe-republish) - refuses the truncate the wrong copy
  would have performed
