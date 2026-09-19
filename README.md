# clonedrift

Name every script on a share that exists in more than one version, and say what the versions
disagree about. Exits non-zero when two copies of one script disagree about a call that destroys
data.

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
PASS  an unknown --min-class is rejected
----------------------------------------------------------------------
172 assertions, 0 failed
```

## Requirements

Python 3.9 or later. Standard library only: `argparse`, `difflib`, `hashlib`, `io`, `json`, `os`,
`re`, `sys` and `tokenize`, with `shutil` and `tempfile` for the self-test's own temporary tree.
No `arcpy`, no `git`, no network and no credentials. It runs the same on ArcGIS Pro's Python and
on a plain `python3`.

```
git clone https://github.com/uhsear/clonedrift.git
python clonedrift.py --self-test
```

The self-test was run on Windows with Python 3.9 and Python 3.13, and on Linux with Python 3.12.
All three report 172 assertions and 0 failed.

## Usage

The path can be a folder or a single file.

```
python clonedrift.py /path/to/share
python clonedrift.py /path/to/share --show-diff
python clonedrift.py /path/to/share --min-class cosmetic --fail-on path
python clonedrift.py /path/to/share --near 0.8
python clonedrift.py /path/to/share --out drift.json --apply
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
| `--self-test` | off | Run the offline assertions and exit. |

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

## Exit codes

0 no group at or above `--fail-on`, 1 at least one group at or above it, 2 the path does not exist
or nothing could be scanned, 64 usage error. A flag value argparse itself rejects, such as a
`--near` that is not a number or an unknown `--min-class`, is argparse's own error and exits 2.

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
