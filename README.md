# ONEIROS

A nightly "dream" pass for Claude Code's file memory. It rereads the Claude Code sessions you had since the last dream, proposes small, source-quoted changes to your auto-memory folder, applies them to a staged copy behind deterministic gates, and leaves a report. Nothing reaches your live memory until you review the dream and swap it in yourself, and every swap can be undone.

Status: experimental. It was built and evaluated by one person on one memory folder (see Evaluation). Read the safety notes before you schedule it.

## How it works

One dream is one folder under `runs/<id>/`:

1. `due`: a dream is due when 24 hours have passed since the last one, at least 3 sessions with your typed turns started since then, and no earlier dream waits for your review.
2. `start`: copies your memory folder to `before/` and `stage/` (with a SHA-256 manifest) and renders each new session transcript to plain text. Your typed lines, the assistant's text and short tool-call hints are kept; tool results are never rendered. Pasted text, IDE context and shell output are marked as pasted, compaction summaries as model context, and credentials of well-known shapes are removed. Assistant and tool lines are cut to fit about 40,000 characters per session; your typed lines are always kept (each up to 1,500 characters).
3. `analyze`: one model call per session proposes candidate facts, each with a verbatim quote and its speaker. A script keeps a candidate only if the quote stands on a line of that speaker and every number of the statement stands within 4 lines of it; a link, address, host name or code span in a fact attributed to you must be something you typed within those lines.
4. `consolidate`: one model call turns the kept candidates into operations on the staged copy: `append`, `supersede` (keeps the old wording in a `History:` line), `conflict` (records a contradiction instead of choosing), `add_file`, `archive` (moves, never deletes) and `set_index_line`.
5. `apply` and `gate`: deterministic code applies the operations one by one and rejects any that break a rule (below), then checks the staged folder as a whole.
6. `report`: writes `REPORT.md` and prints a one-line brief built from counts only.
7. `swap <id>` (you, after review): backs up live memory, then copies the staged folder over it, or re-applies the reviewed operations if live memory changed in the meantime, and re-runs the gate. `undo <id>` restores the backup; `dismiss <id>` drops a dream.

Model steps are headless `claude -p` calls. Each runs in a fresh temporary folder that holds only its inputs, with no CLAUDE.md, user settings, MCP servers or auto-memory, read-only tools (Read, Glob, Grep), a JSON schema for the answer and a spending cap per call (USD 1.50 per analyze call, 6.00 per consolidate call; change them with `--budget`). This isolation relies on the `claude` CLI honouring those flags. The folder, and the trace Claude Code keeps for it under `~/.claude/projects`, are removed after the call.

## Rules the code enforces

- Every write (`append`, `supersede`, `conflict`, `add_file`) cites the sessions and candidate ids it rests on; index lines and archives cite sessions, and archiving a `user` or `feedback` file also needs candidates. Every number and date, and every link, e-mail address or code span, in written text must appear in a cited candidate or in the target file.
- A file of type `user` or `feedback` (a preference or rule about you) is written only from candidates you typed yourself, never from the assistant's words or pasted text.
- Credentials are never written, nor characters a reader cannot see (format, control or private-use characters). Read-only files are left alone. Files you list as protected accept only appends.
- `MEMORY.md` changes only through index lines of at most 200 characters (new ones 150), and the index stays under 25,000 bytes. A replaced index line must keep every number and identifier its topic file lacks, and its warning, correction and prohibition wording ("never", "do not", the warning sign, your own decisions, quoted words). A status may update ("not yet sent" to "sent") only if the status word stays, and a replaced line may not add rule or warning wording that its topic file does not hold.
- At most 40 operations per dream, each writing at most 3,000 characters, so `REPORT.md` always shows everything a swap would write, with each operation's evidence (the speaker and quote of up to six cited candidates, 300 characters each). Symlinks are never followed or written through. Swap and undo run one at a time.
- No em or en dashes in written text, except the separator right after the link in an index line. This is a house style: to drop it, change `DASHES` in `scripts/oneiros.py` and the dash rule in `prompts/consolidate.md`.

## Requirements

- macOS or Linux (file locks use `fcntl`), Python 3.9 or later, standard library only.
- Claude Code with the `claude` CLI on your PATH and signed in. ONEIROS was built against Claude Code 2.1; it reads the session `.jsonl` files Claude Code writes (one compact JSON object per line) and passes these flags to `claude -p`: `--model`, `--effort`, `--setting-sources`, `--settings`, `--no-session-persistence`, `--strict-mcp-config`, `--permission-mode`, `--tools`, `--permission-prompts`, `--output-format json`, `--json-schema`, `--max-budget-usd`. Another version may need `claude_call` in `scripts/oneiros.py` adjusted.
- Cost: about one model call per session plus one or two consolidation calls per dream; in the author's runs a dream over 16 to 20 sessions cost about USD 2.50 to 3.50 in API-equivalent tokens.
- macOS for the optional desktop notification.

What leaves your machine: the rendered session transcripts and your memory files are sent to Anthropic as part of those model calls, exactly as when Claude Code itself reads them.

## Setup

```bash
git clone <this repository> oneiros && cd oneiros
cp oneiros.config.example.json oneiros.config.json
```

Edit `oneiros.config.json`:

- `project`: your Claude Code project folder, for example `~/.claude/projects/-Users-you-Documents` (the folder that holds the session `.jsonl` files).
- `memory`: its auto-memory folder, usually `<project>/memory`. It must hold a `MEMORY.md` (it may be empty).
- `protected`: memory files that may only be appended to, for example your profile. Required: an empty list `[]` says you want none.

The environment variables `ONEIROS_PROJECT`, `ONEIROS_MEMORY`, `ONEIROS_PROTECTED` (comma separated) and `ONEIROS_CONFIG` (another config path) override the file. A config file that cannot be read, or holds the wrong types, stops every command instead of running without protection. Then run the tests:

```bash
python3 -m unittest discover -s tests -q
```

## Usage

```bash
O=scripts/oneiros.py
python3 $O due                         # exit 0 due, 3 not due, 2 needs you (also before the first dream)
python3 $O start --since 2026-01-01    # the first dream needs --since
python3 $O analyze <run>
python3 $O consolidate <run>
python3 $O apply <run>
python3 $O gate <run>                  # exit 1 is a warning, not a failure: still run report
python3 $O report <run>
```

Which sessions a dream reads: those with your typed turns that started on or after the `--since` day (later dreams: the day of the last dream), oldest first, at most 20. The rest wait for the next dream, which continues exactly where this one stopped. A dream is due again after 24 hours and 3 new sessions. For a first run, a recent `--since` is the cheap choice.

Read `runs/<run>/REPORT.md`, then:

```bash
python3 $O swap <run>                  # apply it; --skip 3,7 leaves out operations by their number in the report
python3 $O undo <run>                  # restore the backup (last swapped, first undone)
python3 $O dismiss <run>               # drop it: the nightly is no longer blocked (later dreams start from its day)
python3 $O clean-sandboxes             # list traces older versions left in ~/.claude/projects (--yes deletes them)
```

Exit codes: 0 pass, 1 warn, 2 withhold or error, 3 not due. If three dreams in a row stop before their report, `due` and `start` refuse (exit 2) until you have looked at them and dismissed them.

A nightly run, for cron or launchd, once you have run the first dream by hand with `--since` (it stages and reports, and never swaps):

```bash
cd /path/to/oneiros && O=scripts/oneiros.py
python3 $O due; c=$?; [ "$c" = 3 ] && exit 0; [ "$c" = 0 ] || exit "$c"   # 3 not due; 2 needs you
RUN=$(python3 $O start | python3 -c 'import json,sys; print(json.load(sys.stdin)["run"])') || exit 1
for step in analyze consolidate; do python3 $O $step "$RUN" || python3 $O $step "$RUN" --workers 3 || exit 1; done
python3 $O apply "$RUN" && { python3 $O gate "$RUN"; python3 $O report "$RUN" --notify; }
```

`SKILL.md` is a Claude Code skill for running the same steps from a session or a Claude Code scheduled task. An unattended agent should show only the `brief` field of report's output, and never swap or read run files: they hold text that models and transcripts wrote.

For a backlog, `chain_dream.sh <chain folder>` runs `<chain folder>/plan.json` as chained dreams, each built on the last one's staged copy (`start --after`), so swapping them in order copies each one exactly. The plan is a list of day-aligned batches of at most 20 sessions:

```json
[
 {"id": "B01", "since": "2026-01-05", "until": "2026-01-12", "n": 18, "sids": ["<session id>", "..."]},
 {"id": "B02", "since": "2026-01-13", "until": "2026-01-19", "n": 15, "sids": ["<session id>", "..."]}
]
```

`sids` are the session file names without `.jsonl` (the driver warns if `start` picks a different set). A `STOP` file in the chain folder stops it before the next batch; rerunning it reuses finished batches.

The `mechanical` command, `start --eval` and the probe helpers are hooks the author used for evaluation; evaluation runs are never swapped.

## Safety notes

- Transcripts contain text you did not write: web pages, e-mails, tool output. Pasted spans are handled fail-closed (from the first paste tag of a message to the last closing one counts as pasted, because pasted text can forge tags). The gates above limit what such text can do (it can never become a rule about you, and links or code must be quoted), but a dream can still stage a wrong or planted fact in a project or reference file. That is why the swap is manual. Review each dream, and use `--skip` freely.
- Everything in `runs/` is private: copies of your memory and renders of your sessions. The folder is created readable by you only and is git-ignored.
- An unattended agent that runs the dream should never read `REPORT.md` or other run files, because their text comes from models and transcripts. The `brief` exists for that purpose, and the other commands print only counts, codes and fixed reason classes; the full reasons stay in the run's files. In `REPORT.md` itself, invisible characters are removed and links are made inert.

## Evaluation

The author evaluated the design on their own memory before using it, with pre-registered rules. On about 90 frozen probe questions drawn from 20 sessions, an answerer with the dreamed memory got about 8 more questions right per run than with the memory from before the dream (p < 0.001, one-sided permutation test). A placebo dream over unrelated sessions gained about 1, and a no-model control that only appended facts about 2, neither significant. Facts that had to survive did (within one probe). These are results on one person's private data; they cannot be reproduced from this repository, and the probe harness is not included.

Known limits:

- The gain was in retaining new facts. Updating stale memory, where a dream should help most, was not measured.
- A dream over old sessions against an already rich memory writes much more repetition than new fact: in one backlog run over more than 200 sessions, an independent audit rated only about a quarter of the staged changes as new, correct and useful. Review backlog dreams with care.
- The index guard counts words; it does not read meaning.
- The gates check numbers, links, code, speakers and provenance, not meaning: a dream can still paraphrase freely while citing one real quote of yours. The evidence lines under each operation in `REPORT.md` let you compare the claim with the quote before you swap.
- A fact attributed to you that names a host, an e-mail address or code is kept only if you typed it near the quote; in the author's data about 1.5% of candidates were dropped this way, mostly hosts and addresses the assistant had mentioned.

## License

Copyright 2026 AKINTECH Driven Ltd. Licensed under the Apache License, Version 2.0: see `LICENSE`. If you redistribute ONEIROS or a work derived from it, keep the `NOTICE` file with it.
