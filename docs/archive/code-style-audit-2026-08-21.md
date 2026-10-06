# Code style audit: identifier names and code shapes

Date: 2026-08-21. Base commit: 17f72a5d.

## Scope

Every tracked Python file. This audit covers identifier names and code shapes.
The comment and docstring text is covered by a separate audit.

The repository is a hard fork. Line-level blame attributes every candidate to
the commit that wrote it, and upstream code stays as upstream wrote it.

## What it looks for

A name says what the value is. A shape says what the code does in the plainest
way the language offers. The scan looks for names that repeat their type,
names that hold a placeholder, and shapes that take more words than the thing
they express.

## Method

1. Scan. A syntax-tree walk flags candidates. It reads structure, not text, so
   it does not match names inside strings or comments. The walk produced 438
   candidates at distinct sites.
2. Attribute. Blame assigns 110 to upstream and 328 to this fork.
3. Judge. A reader compares each candidate against the surrounding code and
   the class that owns it, then confirms or rejects it.
4. Fix. A confirmed candidate gets the smallest edit that states the thing
   plainly.
5. Prove. The scenario suite is the safety net here. The syntax-tree proof
   that protects a comment sweep does not apply, because changing code is the
   point of this audit.

## Safety rules

Renaming can break code that a symbol search cannot see. Four rules bound the
work, and a candidate that fails any of them is recorded rather than changed.

1. No rename under the plugin API surface. Those names reach separate
   repositories.
2. Only a local variable, or a parameter of a module-private function whose
   every call site the reader has read. No class, no method, no module-level
   name, no instance attribute.
3. Before any rename, search the whole repository for the old name as a string
   literal. A name reached through attribute lookup by string, a signal
   connection, a settings key, an event id or a manifest key stays as it is.
4. No signature change and no behaviour change.

## Findings

| Signal | Candidates | Confirmed | Rejected |
| --- | ---: | ---: | ---: |
| except-pass | 115 | 0 | 115 |
| name-type-suffix | 101 | 16 | 85 |
| self-getattr-literal | 37 | 0 | 37 |
| trivial-forwarder | 34 | 1 | 33 |
| len-compare-zero | 22 | 15 | 7 |
| append-loop | 7 | 2 | 5 |
| name-placeholder | 7 | 2 | 5 |
| assign-then-return | 2 | 2 | 0 |
| else-after-return | 2 | 0 | 2 |
| iterate-keys | 1 | 1 | 0 |
| **Total** | **328** | **39** | **289** |

The self-getattr-literal row counts two builtins. Thirty-five sites call
getattr with a default, and two call hasattr. The signal name says getattr
alone, which understates what it measures.

**name-type-suffix.** The shape was a name that repeats the type of the value: `plugin_obj`, `page_object`, `page_obj`, `input_obj`, `state_obj`, `dc_list`, `ident_str`. Each one became the domain word: `plugin`, `page`, `controller_input`, `state`, `controllers`, `ident`. A second group named a mapping after its container type when only one mapping was in scope. `input_dict` became `config` at the two `get_config` call sites and `input_config` in `ensure_state_dict`. `target_key_dict` and `dropped_key_dict` became `own_content` and `dropped_content`. `state_dict` became `state_actions` where the dict held actions.

**len-compare-zero.** The shape was `len(x) == 0` or `len(x) > 0` on a plain list, dict or set. Emptiness checks became `not x`, non-empty checks became `x`, and the one predicate that must return a bool became `bool(...)`. The sites were the two error checks in `MainWindow.check_for_errors`, the commit list in `StoreBackend`, `LabelManager.get_has_scroll_labels`, the default page pick in `DeckController`, an action count in `RemoveButton`, and eight test assertions.

**append-loop.** The shape was a list filled by a loop that appends one item per pass. `DeckManager` now builds `loaded_deck_ids` with a comprehension. `GnomeExtensions.get_extensions` now calls `extensions.extend()` on the unpacked D-Bus reply.

**assign-then-return.** The shape was a local that is assigned and then returned on the next line, with no use in between. `DynamicFlowBox.get_items_to_show` and `ControllerInputState.get_own_actions` now return the expression directly.

**name-placeholder.** The shape was a bare `tmp` for a value that is not a scratch scalar. The temporary marker file in `rebrand_migration._write_marker` is now `tmp_path`. The fake source tree in `check_settings_json.self_test` is now `tree_root`.

**trivial-forwarder.** The shape was a module-private wrapper that forwards its arguments unchanged. `_find_font_path` in `KeyLabel.py` passed three arguments straight to `font_resolver.resolve` and had one caller. The wrapper is gone and its cache note now sits at the call site.

**iterate-keys.** The shape was `for state in self.states.keys()` in `ControllerInput.add_state`. The `.keys()` call is gone.

## Accepted exceptions

**Half-built teardown sweeps.** `DeckController.close()` and `_teardown_failed_init` run against an object whose `__init__` may have raised before the attribute existed, so the `getattr` defaults are control flow, and the source states that contract inline.

**Lazily created slots.** Attributes such as `_last_img_hash` and `_last_enqueued_hash` carry a bare annotation and get their first value on the paint path, so a strict read would raise before the first paint.

**Deleted attributes.** `close()` deletes `self.image` outright, and the declaration says every reader guards on both `hasattr` and `None`, so the guard is part of the object's lifecycle.

**Expected-exception assertions.** Test code uses `try/except/else` where the `else` raises `AssertionError`, so the `pass` is the success path of the check, not a swallowed error.

**Deliberate best-effort swallows.** A fallback device close on a wedged writer, a fixture that truncates a response and drops the connection, and a failure path that must let the construction error propagate all drop the secondary exception on purpose.

**Public and plugin-facing names.** Method names, `PluginManager` files, and parameters of overridden methods such as `load_from_input_dict` fall under the safety rules, because a rename there breaks callers, subclasses or the sibling plugin repos.

**Callbacks that add arguments.** The `filter_func` and `sort_func` handlers registered with Gtk supply the live search text and `ASSET_PATH_ATTR`, and the coordinate helpers supply `self.deck`, so none of them forward their arguments unchanged.

**Suffixes that carry a distinction.** `page_dict` names a page's raw JSON mapping against a `Page` object, and `state_dict` sits beside a live `state` object in the same loop body, so the suffix separates two things rather than repeating one type.

**gi objects without container truthiness.** A `Gtk.SelectionModel` over a `Gio.ListModel` is lazily sized and is not a plain container, so `len(...) == 0` is not interchangeable with a truth test.

**Paired alternatives.** The color resolution chain offers three mutually exclusive sources under one explanatory comment, so flattening the `else` would split the explanation from two of its branches.
## Verification

The gate reports zero from both checkers, a clean linter, clean module-size,
eager-annotation and any-explicit guards, and a clean byte-compile. The
scenario suite passes in full.

Three further checks cover the safety rules directly.

- No file under the plugin API surface appears in the change.
- Seventeen identifiers vanish between the two revisions. A search for each one
  as a string literal returns one hit, and that hit names an input type in an
  unrelated list. The renamed names appear in no string.
- Three signature lines change. One belongs to a deleted wrapper whose only
  caller now calls the resolver directly. Two rename a parameter of two
  module-private helpers inside one test file, and no caller passes those
  parameters by keyword.

## What this audit got wrong

The first version of this document reported 455 candidates, 344 of them from
this fork, and a rejected count of 305. The scan walked a nested function once
for itself and once again as part of its enclosing function, so fifteen sites
appeared twice. Counting distinct sites gives 438, 328 and 289. Review caught
the discrepancy through the getattr row, and the recount corrected every row
it touched.

The signal named self-getattr-literal also counts hasattr calls, which its
name does not say. The table now states the split.

No fix changed. The duplicates named sites that were already judged, so the
recount moves counts and leaves the 39 confirmed fixes as they were.

## Known gaps

The scan reads shapes it was taught to recognise. It cannot see a function
that does too much, a class that holds unrelated state, or an abstraction that
earns nothing. Those need a reader, not a pattern.

Two hundred and eighty-nine candidates were rejected. Most of them are the
never-raise handlers and the late-bound attribute reads described above. They
are design decisions, and a later change to any of them belongs with the code
that owns the decision rather than with a style sweep.
