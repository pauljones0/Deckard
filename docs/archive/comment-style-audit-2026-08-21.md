# Comment and docstring style audit

Date: 2026-08-21. Base commit: 3bd36991.

## Scope

Every tracked Python file. The audit covers comment text and docstring text.
It covers no code.

The repository is a hard fork. Upstream text stays as upstream wrote it, so
the audit attributes every candidate line to the commit that last wrote it and
keeps only the lines this fork authored. Quoted licence notices stay verbatim,
whoever committed them.

## Rules applied

A comment describes the code or states a present-tense constraint. It stands
on its own.

1. No tracker references. No campaign, process or history narrative.
2. No first person.
3. No em dash, no en dash, no double-hyphen splice.
4. No section banners built from dashes, equals signs or box characters.
5. No capital-letter emphasis. Identifiers and constants in capitals are fine.
6. No markdown backticks around prose.
7. No agent-voice lexicon, such as "load-bearing", "by construction",
   "on purpose", "the whole point", "best-effort" and "deliberately".
8. No "not X, but Y" antithesis framing.
9. No label-colon openers, such as "Edge:" or "Snapshot:". Google-style
   docstring sections and Sphinx fields are exempt.
10. Sentence discipline from ASD-STE100. One idea per sentence, active voice,
    simple tense, and about 25 words at most for a description.

## Method

1. Extract. A tokenizer collects every comment token. The syntax tree collects
   every module, class and function docstring. The tree holds 13,372 comment
   lines and 2,223 docstrings across 467 files.
2. Scan. Regular expressions flag candidates for each rule. The scan generates
   candidates and never verdicts. It over-reports on purpose, because a narrow
   scan misses real text.
3. Attribute. Line-level blame maps each candidate to its author commit. The
   audit drops the candidates that upstream wrote.
4. Judge. A reader compares each surviving candidate against the surrounding
   code, then confirms or rejects it. A rejection records why the text is
   legitimate.
5. Fix. A confirmed finding gets a rewrite that keeps every fact the original
   asserted. The rewrite changes no modality. It widens no enumeration and it
   narrows none.
6. Prove. Both versions of every touched file parse. The check erases all
   docstrings from both trees, then compares them. Comments never reach a
   tree, so a comment edit passes automatically. Only a code edit fails this
   check.

## Results

The scan produced 796 raw candidates. Blame assigned 341 to upstream and 455
to this fork. Two review passes judged the fork's candidates and fixed 373 of
them. A third pass by hand closed twelve items that the passes left
inconsistent with their own decisions.

Two different quantities appear below. Keep them apart. The first counts
comment blocks that hold a given pattern. The second counts sentences.

Counts exclude the quoted licence header, which trips several rules in every
file and stays verbatim.

| Rule | Comment blocks before | Comment blocks after |
| --- | ---: | ---: |
| dash-splice | 17 | 0 |
| banner | 16 | 0 |
| antithesis | 17 | 15 |
| claudism | 8 | 0 |
| caps-emph | 2 | 0 |
| backtick | 30 | 30 |
| first-person | 24 | 24 |
| process | 11 | 11 |
| label-open | 10 | 10 |
| arrow-chain | 4 | 4 |

Sentence length is counted separately, one count per sentence rather than one
per comment, across every tracked file rather than across a candidate list.
Fork-authored sentences longer than 25 words fell from 972 to 649. Of the 649
that remain, 409 sit between 26 and 30 words, 216 between 31 and 40, and 24
above 40.

The dash splice, the banner, the lexicon terms and the capital emphasis are
gone from the tree. The antithesis rule keeps a count because its remaining
candidates were all rejected as direct invariants. The backtick, first-person,
process, label-colon and arrow rules keep their counts for the same reason,
and the next section says why each is legitimate.

## What the fixes changed

**Long sentences.** The common shape chained two or three claims with a comma
and "so", "and" or "because". The fix split the chain at the joint and gave
the second claim its own sentence. A causal link survives as "therefore" or
"then". Where a docstring buried a list inside a subordinate clause, the list
became its own sentence.

**Section banners.** Fifteen decorative rules framed section titles. Two
shapes appeared: a boxed title in the deck controller, and a full-width rule
around each part heading in a scenario. The fix deleted the rule lines and
kept the title as a plain comment.

**Dash splices.** Seventeen comments used a double hyphen as a general joint.
The fix chose punctuation that matches the relation. A colon introduces an
explanation, a comma introduces a cross-reference, and parentheses enclose an
aside. In running prose the splice became a sentence break.

**Lexicon.** Six comments asserted that something mattered instead of saying
what it does. "The whole point" became a statement of what the code records.
"By construction" became a statement of the mechanism.

**Antithesis.** Three comments built a contrast, then spent a clause knocking
down the half they rejected. The fix keeps the true half and drops the frame.

**Capital emphasis.** Two comments shouted one word for stress. Lower case
replaced the capitals. Both comments already name their two events in order,
so the ordering claim survives without the emphasis.

## Accepted exceptions

**Quoted licence notices.** Every module carries the GPL notice. Its capitals,
its author and year fields and its redistribution clause trip three rules
each. The text is quoted, so it stays byte-exact.

**Mirrored upstream API docstrings.** The deck wrapper mirrors the StreamDeck
library method by method, and it repeats that library's docstrings. A rewrite
would make the mirror diverge from the interface it documents.

**Code tokens inside prose.** Backticks around real code, such as
`if TYPE_CHECKING` or `# type: ignore`, mark a token rather than decorate
prose. The backticks also stop an embedded hash from reading as a second
comment marker.

**Usage synopses and worked examples.** A command line with angle-bracket
placeholders, and a worked value derivation in a test, are sample text rather
than authored sentences.

**Docstring section headings.** Lines such as "Why this module exists" and
"What a read hands back" are headings in a docstring's own structure.

**Domain phase names.** A transition's "Phase 1" and "Phase 3" name the steps
of the algorithm the comment describes. They carry no project narrative.

**Mapping arrows.** A line such as "input type to identifier to state to
action" reads as a chain, and the scan flags it. The arrow notation states the
shape of a nested registry, and prose would state it less clearly.

**Bare negatives.** A statement such as "one raiser must not starve its
siblings" is a direct invariant. The scan matched the word "not" with no
contrasting clause present.

**Unit and pronoun collisions.** The microsecond unit, the phrase "file I/O",
and a quoted question inside a docstring all matched the first-person rule
without containing first person.

**Scanner join artifacts.** The scan fuses an indented command sample or a
docstring heading with the prose beside it, then reports the fused result as
one long sentence. The real sentences are short.

## Comments that state something untrue

The audit found none. Every reader who compared a flagged comment against its
implementation confirmed the stated behaviour. Both passes were instructed to
report a discrepancy rather than to correct it silently, and neither pass
reported one.

## Proof that no code changed

The check parses the committed version and the working version of all 149
touched files, erases every docstring from both trees, and compares them. All
149 files match.

The check earned its place. One edit in this audit replaced a comment marker
with plain text and broke the file. The comparison caught it before any
commit, and the marker went back.

## Known gaps

Six hundred and forty-nine fork-authored sentences still run past 25 words.
Four hundred and nine of them sit between 26 and 30 words. The sweep fixed 327
sentences, so it addressed a third of the population rather than most of it.

The reason is the candidate generator, not the fixes. The generator reported
at most one sentence per comment block, and the review then read only the
blocks it named. A comment holding three long sentences therefore surfaced
once and could leave two behind. A later pass should count sentences, walk
every file, and work from that list.

Fifteen antithesis candidates remain. Each states a direct invariant that
happens to contain the word "not".

The scan measures comment text alone. It cannot judge whether a comment earns
its place, and it cannot see a comment that should exist and does not.

## What this audit got wrong

The first version of this document reported that sentences over 25 words fell
from 306 to 44. Both figures were wrong. They counted comment blocks rather
than sentences, and they counted only inside the files that the candidate scan
had already flagged. Review caught the error, and independent re-measurement
put the true figures at 972 and 649.

The same version claimed that every rule with a confirmed finding now reads
zero. Its own table contradicted that, because the sentence-length and
antithesis rows were never zero. Both statements are gone.

Review also found three banner comments that the scan missed, because the
pattern required a longer run of dashes than those lines carried. They are
fixed, and the banner count above reflects it.
