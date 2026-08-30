# Python comment review campaign

## Purpose

Review every current Python comment and docstring that Deckard authored after
the StreamController fork boundary. Check correctness, need, length, clarity,
and consistency with the original StreamController style.

The review covers production code, tests, scripts, tooling, `GtkHelper`,
`locales`, Flatpak Python files, and root Python modules. It does not cover Git
commit messages, GitLab text, deleted comments, or unchanged upstream prose.

The initial snapshot is `c2d048bb`. The fork boundary is `129bdb58`. A final
delta pass will include Python prose added after the initial snapshot.

## StreamController baseline

The fork-boundary tree contains 216 Python files. Python tokenization finds
1,540 ordinary comment blocks after pragmas, attribution text, and probable
commented-out code are excluded.

| Block size | Count | Share |
| --- | ---: | ---: |
| One line | 1,439 | 93.44% |
| Two lines | 68 | 4.42% |
| More than two lines | 33 | 2.14% |

The baseline uses these patterns:

- A terse action phrase or noun label is common.
- A complete sentence and terminal period are not mandatory.
- A reason or constraint sits directly before the affected code.
- An inline comment identifies a field, unit, argument, or unusual value.
- A local API docstring uses a short third-person summary.
- Structured `Args`, `Returns`, and `Raises` sections occur where an interface
  needs them.

The baseline also contains text that the campaign must not copy: decorative
banners, commented-out implementations, stale TODO blocks, spelling errors,
and inconsistent capitalization.

Licence text, tool pragmas, source attribution, and externally mirrored API
documentation are protected content. They are not writing examples.

## Initial corpus

The current tree has 626 tracked Python files. The attributed corpus contains
10,891 units in 603 files.

| Type | Units |
| --- | ---: |
| Ordinary comment blocks | 6,792 |
| Pragmas | 622 |
| Shebangs | 22 |
| Commented-out code blocks | 3 |
| Ordinary docstrings | 3,416 |
| Licence-bearing docstrings | 36 |

Only 2,377 ordinary comment blocks use one line. Another 1,935 use two lines,
and 2,480 use more than two lines. Of the ordinary docstrings, 655 use one
physical line, 366 use two, and 2,395 use more than two.

## Attribution and extraction

1. Enumerate tracked `*.py` files from the review revision.
2. Extract `#` comments with Python tokenization.
3. Extract module, class, function, and method docstrings from the syntax tree.
4. Group adjacent standalone comments only when indentation and type match.
5. Keep inline comments as separate units.
6. Attribute each physical line with move-aware and copy-aware Git blame.
7. Include a unit when at least one line is newer than `129bdb58`.
8. Keep boundary lines only as context in a mixed-attribution unit.

The inventory must fail if a tracked Python file does not parse, tokenize, or
blame. It must report the pre-attribution count, attributed count, mixed-unit
count, and partition sum.

Git blame assigns an inline comment with its code line. A reviewer must compare
an uncertain inline comment with the boundary version before treating it as
Deckard text. Short copied text can also escape Git copy detection. The review
must classify such text as protected when context proves its upstream origin.

## Review rules

1. Read each unit with the code that it describes.
2. Keep a comment only when it adds information that the code does not show.
3. Confirm that the comment describes current behavior.
4. State the current rule, reason, condition, bound, order, or failure mode.
5. Do not mention former behavior, project history, plans, reviews, issues,
   merge requests, or commits.
6. Use the terse StreamController action phrase, noun label, or short summary
   style where it fits.
7. Use one physical content line by default.
8. Use two physical content lines only when the second line keeps a necessary
   reason, condition, bound, order, or failure mode.
9. Rewrite or delete Deckard-authored prose that uses more than two content
   lines. Protected text is exempt.
10. Do not require a terminal period or force a fragment into a full sentence.
11. Keep each condition, bound, scope qualifier, and concurrency invariant.
12. Do not widen or narrow an enumeration during a rewrite.
13. Do not change pragma syntax, shebangs, licence text, or mirrored upstream
    documentation.
14. Review an explanatory suffix on a pragma as ordinary prose.
15. Delete commented-out code unless current code still needs it as an example.

A physical content line excludes a docstring delimiter-only line. A one-line
docstring has its text and delimiters on one physical line. A two-line
docstring can use two text lines plus delimiter-only lines.

## Review outcomes

Each unit gets one outcome:

- **Keep**: correct, useful, compact, and consistent with the baseline.
- **Rewrite**: useful, but incorrect in form, unclear, historical, or too long.
- **Delete**: redundant, stale, empty, or commented-out code with no current use.
- **Protected**: upstream, licence, pragma syntax, shebang, attribution, or
  externally mirrored API text.
- **Defect**: the comment and code disagree, and the correct behavior is not
  clear from local evidence.

A reviewer must not hide a defect with a prose edit. The reviewer must open a
separate defect issue and leave the unit unchanged until the behavior is known.

## Complete partition map

The inventory assigns each initial unit to one partition.

| ID | Scope | Files | Units |
| --- | --- | ---: | ---: |
| P00 | Support modules, GtkHelper, locales, scripts, Flatpak | 43 | 397 |
| P01 | Deck lifecycle and input contract | 17 | 604 |
| P02 | Media writer and paint protocol contract | 8 | 159 |
| P03 | Render composition and cache contract | 29 | 627 |
| P04 | Public plugin API contract | 4 | 163 |
| P05 | Signals and event dispatch contract | 6 | 102 |
| P06 | Plugin manager internals | 10 | 108 |
| P07 | Atomic settings and page persistence contract | 7 | 239 |
| P08 | Page management and migration | 7 | 211 |
| P09 | Store backend | 14 | 320 |
| P10 | Asset and pack backends | 13 | 105 |
| P11 | Desktop and platform integration | 15 | 233 |
| P12 | Asset and store UI | 31 | 410 |
| P13 | Main-window UI | 28 | 336 |
| P14 | Other windows and UI adapters | 19 | 194 |
| P15 | Application shell and core backend services | 21 | 569 |
| T00 | Test harness, hardware, and soak tests | 19 | 227 |
| T01 | Scenarios A | 20 | 512 |
| T02 | Scenarios B | 14 | 233 |
| T03 | Scenarios C | 16 | 502 |
| T04 | Scenarios D through E | 30 | 627 |
| T05 | Scenarios F through H | 32 | 493 |
| T06 | Scenarios I through K | 20 | 453 |
| T07 | Scenarios L through O | 33 | 531 |
| T08 | Page scenarios | 21 | 406 |
| T09 | Other P scenarios | 18 | 337 |
| T10 | Scenarios Q through R and W through X | 27 | 422 |
| T11 | Store scenarios | 25 | 448 |
| T12 | Other S scenarios | 30 | 435 |
| T13 | Scenarios T through V | 26 | 488 |

The partition sum is 10,891 units. Contract partitions P01 through P05 and P07
require the repository contract-surface review. A file belongs to one initial
partition only.

## Delivery sequence

### Stage 1: Reproducible inventory

Create a standard-library-only inventory tool. Keep each module below 300
lines. The tool prints a selected partition in review-sized chunks and emits a
summary with exact coverage counts. Add focused self-tests for extraction,
attribution, mixed units, and partition exclusivity.

### Stage 2: Pilot review

Review P00 first. Use it to confirm the outcome record, rewrite process,
syntax-tree comparison, and issue template. Change the process before the
contract partitions start if the pilot finds a coverage gap.

### Stage 3: Production waves

Create one issue, branch, worktree, and merge request for each P partition.
Each issue must contain the exact file set and expected unit count. Independent
partitions can run in parallel after the pilot.

### Stage 4: Test waves

Run T00 through T13 with the same rules. Test comments are not exempt from
correctness, need, length, or historical-narrative review.

### Stage 5: Final delta

After all initial partitions merge, rerun the inventory on current mainline.
Review every attributed unit added or changed after `c2d048bb`. Re-review a
unit when conflict resolution or another merge changed its text.

### Stage 6: Campaign closure

Prove that every initial and delta unit has one outcome. Run the full gate on
the merged mainline. Close related residual comment issues only when their
listed work is present on mainline.

## Verification

For every prose-only change:

1. Parse the base and changed Python files.
2. Normalize all docstring nodes.
3. Compare the normalized syntax trees.
4. Require equality for files with no deliberate code change.
5. Inspect the diff for lost conditions, bounds, qualifiers, and enumerations.
6. Run `ruff check .`.
7. Run `ty check`.
8. Run `python scripts/check_module_sizes.py`.

Do not run scenario or unit tests on the local host for prose-only changes.
Normalized syntax-tree equality is the runtime-behavior proof for those diffs.
Run tests for inventory-tool code on the serialized Hugo test host.

The campaign does not change runtime behavior. A partition that needs a code
fix must move that fix to a separate defect issue and merge request.

## Acceptance criteria

- When the inventory runs on a revision, the tool shall account for every
  tracked Python comment token and syntax-tree docstring.
- When attribution completes, the tool shall separate fork-authored lines from
  unchanged StreamController lines with move-aware and copy-aware blame.
- When partitioning completes, each attributed unit shall belong to exactly
  one partition.
- When a reviewer handles a unit, the review shall assign one defined outcome.
- When a comment is useful, the retained text shall follow the StreamController
  style measured at the fork boundary.
- When a comment needs a rewrite, the rewritten text shall use one content line
  or two content lines with a necessary reason for the second line.
- When a comment only repeats code, the change shall delete the comment.
- When a comment describes history, the change shall state only the current
  rule or reason.
- When a comment and code disagree without clear local evidence, the reviewer
  shall open a separate defect issue and leave behavior unchanged.
- When a prose-only change is ready, the normalized syntax trees shall match.
- When all initial partitions merge, the campaign shall review the mainline
  delta from `c2d048bb`.
- When the campaign closes, the initial and delta coverage counts shall have no
  missing or duplicate units, and the full repository gate shall pass.
