#!/usr/bin/env python3
"""ONEIROS: a dream pass for Claude Code's file memory (see README.md).

Deterministic orchestrator. Model steps are isolated headless `claude -p` calls; everything that decides what is
allowed (sessions, quotes, operations, gates, swap) is code here. Stdlib only, Python 3.9.

Exit codes: 0 pass, 1 warn, 2 withhold or error, 3 not due.
"""
import argparse
import concurrent.futures as cf
import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser('~')
CONFIG_FILE = os.environ.get('ONEIROS_CONFIG') or os.path.join(ROOT, 'oneiros.config.json')


def _config():
    """Local settings (git-ignored): {"project": Claude Code project folder, "memory": its memory folder, "protected":
    [memory files that accept only append]}. Environment variables ONEIROS_PROJECT, ONEIROS_MEMORY and ONEIROS_PROTECTED
    (comma separated) take precedence. Returns (config, error): a file that exists but cannot be read, or holds the
    wrong types, is an error, so append-only protection never fails open."""
    if not os.path.exists(CONFIG_FILE):
        return {}, None
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return {}, 'oneiros.config.json cannot be read: %s' % e
    if not isinstance(cfg, dict):
        return {}, 'oneiros.config.json must hold a JSON object'
    for k in ('project', 'memory'):
        if k in cfg and not isinstance(cfg[k], str):
            return {}, 'oneiros.config.json: "%s" must be a string' % k
    if 'protected' in cfg and not (isinstance(cfg['protected'], list) and all(isinstance(x, str) for x in cfg['protected'])):
        return {}, 'oneiros.config.json: "protected" must be a list of file names'
    return cfg, None


CONFIG, CONFIG_ERROR = _config()
PROJECT = os.path.expanduser(os.environ.get('ONEIROS_PROJECT') or CONFIG.get('project') or '')
LIVE_MEMORY = os.path.expanduser(os.environ.get('ONEIROS_MEMORY') or CONFIG.get('memory')
                                 or (os.path.join(PROJECT, 'memory') if PROJECT else ''))
PROTECTED_ORDER = [x.strip() for x in (os.environ['ONEIROS_PROTECTED'].split(',') if 'ONEIROS_PROTECTED' in os.environ
                                       else CONFIG.get('protected') or []) if isinstance(x, str) and x.strip()]
PROTECTED_CONFIGURED = 'ONEIROS_PROTECTED' in os.environ or 'protected' in CONFIG
RUNS = os.path.join(ROOT, 'runs')
STATE = os.path.join(ROOT, 'state.json')
PROMPTS = os.path.join(ROOT, 'prompts')

MIN_HOURS = 24
MIN_SESSIONS = 3
LOCK_STALE_HOURS = 6
RENDER_CAP = 40000
USER_LINE_CAP = 1500
ASSISTANT_LINE_CAP = 1200
PASTE_CAP = 300
INDEX_BYTES_CAP = 25000
INDEX_LINE_CAP = 200
NEW_INDEX_LINE_CAP = 150
MAX_OPS = 40
GROUND_RADIUS = 4
SKIP_REASONS = ('one-off', 'not durable', 'contradicts CLAUDE.md', 'duplicate of another candidate')
MIN_EVIDENCE = 12
MIN_QUOTE = 8
PROTECTED = set(PROTECTED_ORDER)  # memory files that accept only append (configured, see _config)
MAX_OP_CHARS = 3000  # characters one operation may write; REPORT.md always shows all of it
MAX_SESSIONS = 20  # sessions per dream; the rest wait for the next run (the evaluated batch size)
MAX_STOPPED_RUNS = 3  # dreams in a row that stopped before their report; then due and start refuse
RUN_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
DASHES = ('—', '–')
STR_FIELDS = ('file', 'text', 'old', 'new', 'date', 'line', 'name', 'description', 'type', 'body', 'reason', 'role')
OPS = ('add_file', 'append', 'supersede', 'conflict', 'archive', 'set_index_line')
TYPES = ('user', 'feedback', 'project', 'reference')
FILE_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,80}\.md$')
LINK_RE = re.compile(r'\]\(([^)\s]+\.md)\)')
INDEX_LINE_RE = re.compile(r'^- \[[^\]]+\]\(([^)\s]+\.md)\)')
# Index guard: wording a replaced index line must keep, as (label, pattern) pairs counted on the case-folded
# line outside link targets, each as a standalone phrase (not inside a path or a hyphenated word). Calibrated on index
# edits from earlier runs, then revised after adversarial review.
# Plain 'only', 'no', 'nothing', 'user' and plural 'rules' mostly mark status and are not counted. 'not' before a status
# word ('NOT sent', 'not yet merged', "isn't live") is not counted either, so a status update can land, but that status
# word must survive in the new line, so a dropped 'not shared outside the team' is still caught. A deny-listed word
# straight after 'not' ('not allowed', 'not required') stays a counted prohibition. Prohibitions ('never', 'do not',
# "can't") and contrasts ('size, not count') count. The two negation labels are matched in one left-to-right pass (NEG_RE),
# so 'do not' counts once and a new status 'not' cannot stand in for a lost 'never'.
_DENY = r"(?:allowed|permitted|authori[sz]ed|recommended|supported|required|advised|intended|approved)(?![\w'-])"
_STATUS = (r"(?:\w+(?<!e)ed|agreed|sent|done|made|built|written|taken|given|shown|seen|known|gone|paid|held|told|read|"
           r"set|put|kept|left|met|found|won|sold|bought|spent|lost|chosen|run|begun|frozen|hidden|drawn|driven|ready|"
           r"live|complete|available|working)")
_ADVERB = (r"(?:yet|been|be|being|fully|currently|still|ever|even|quite|officially|formally|actually|really|properly|"
           r"publicly|all)")
STATUS_TAIL_RE = re.compile(r"\s+(?P<adv>(?:%s\s+)*)(?P<word>%s)(?![\w'-])" % (_ADVERB, _STATUS))
NEVER, NOT = 'never or do not', 'not'
GUARD_PATTERNS = (
    (NEVER, r"never|(?:must|do|does|should|shall|may|can|will|would|need|might|could)\s+not|"
            r"(?:do|does|ca|wo|sha|should|must|would|could|need|might)n't|cannot"),
    (NOT, r"not|(?:is|was|are|were|has|have|had|did|ai)n't"),
    ('must', r"must(?!\s+not(?![\w/-])|n't)"),
    ('always', r'always'),
    ('corrected', r'correct(?:ed|ion|ions)'),
    ('wrong', r'wrong'),
    ('beware', r'beware'),
    ('avoid', r'avoid(?:s|ed)?'),
    ('confidential', r'confidential'),
    ('rule', r'rule'),
    ('⚠ or warning', '⚠|warning'),
    ('user said or decided', r"user(?:'s)?\s+(?:corrected|correction|states?|stated|said|says|vouch(?:es|ed)?|asked|asks|"
                             r"wants?|wanted|decided|decides|decision|chose|choice|call|word|rule|insists?|insisted|"
                             r"approved|approval|declined|confirmed)|user\s+\d{1,2}\s+[a-z]{3}"),
)
_EDGE = r"(?<![\w/.~-])(?:%s)(?![\w/-])"
NEG_RE = re.compile(_EDGE % ('(?P<never>%s)|(?P<not>%s)' % (GUARD_PATTERNS[0][1], GUARD_PATTERNS[1][1])))
GUARD_RES = tuple((label, re.compile(_EDGE % pat)) for label, pat in GUARD_PATTERNS[2:])
WARN_SIGN = '⚠'


# ---------- small helpers ----------

def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec='seconds')


def parse_time(s):
    if not s:
        return None
    s = s.replace('Z', '+00:00')
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def load(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.oneiros-', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def flocked(path, wait=True):
    """An exclusive flock on `path` (created if missing) for the duration of the block; yields False when `wait` is
    False and another process holds it. The lock dies with its process, so it never goes stale."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


@contextlib.contextmanager
def state_update():
    """Read, change and write state.json under one lock, so two commands never lose each other's changes."""
    with flocked(STATE + '.lock'):
        state = load(STATE, {})
        yield state
        save(STATE, state)


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def write(path, text):
    """Write through a temporary file in the same folder and os.replace, so a crash never leaves a half-written file.
    An existing file keeps its permission bits; a symlink is refused (its target may sit outside the folder)."""
    if os.path.islink(path):
        raise OSError('refusing to write through a symlink: %s' % path)
    folder = os.path.dirname(path) or '.'
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(prefix='.oneiros-', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def emit(obj, code=0):
    print(json.dumps(obj, indent=1, ensure_ascii=False))
    return code


def squash(text):
    return re.sub(r'\s+', ' ', text or '').strip()


INVISIBLE_EXTRA = set(range(0xFE00, 0xFE10)) | set(range(0xE0100, 0xE01F0)) | set(range(0x180B, 0x1810)) | \
    {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0, 0x2800, 0xFFFC}
EMOJI_SELECTORS = {0xFE0E, 0xFE0F}  # text and emoji presentation selectors (as in the warning sign): allowed in memory text


def _visible(s):
    out = []
    for ch in unicodedata.normalize('NFC', s):
        if unicodedata.category(ch) in ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp') or ord(ch) in INVISIBLE_EXTRA:
            out.append(' ')
        else:
            out.append(ch)
    return ''.join(out)


def strip_invisible(s):
    """`s` without the characters _visible() would blank (deleted, not spaced), for matching links hidden by them."""
    return ''.join(ch for ch in s if not (unicodedata.category(ch) in ('Cf', 'Cs', 'Co', 'Cn') or ord(ch) in INVISIBLE_EXTRA))


def clean(text, cap):
    """Model-derived text for a report, a notification or command output: control, format, private-use, unassigned,
    zero-width, bidirectional and variation-selector characters removed, whitespace collapsed, at most `cap`
    characters."""
    s = squash(_visible(str(text if text is not None else '')))
    return s if len(s) <= cap else s[:max(0, cap - 3)].rstrip() + '...'


def inert(text, cap):
    """clean(), and markdown links (inline or reference), images, HTML and bare autolinks made inert, for text quoted in
    REPORT.md."""
    return clean(text, cap).replace('[', '\\[').replace(']', '\\]').replace('<', '&lt;').replace('://', ':/ /')


def _emoji(ch):
    return unicodedata.category(ch) == 'So' or 0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF


def hidden_chars(text):
    """Characters a reader would not see (format, control, private-use, unassigned, variation selectors): memory text
    may not hold them, since a later session reads what the reviewer could not. An emoji selector (FE0E, FE0F) right
    after an emoji, and a zero-width joiner between two emoji, are allowed; a run of them is not."""
    s = unicodedata.normalize('NFC', text or '')
    out = set()
    for i, ch in enumerate(s):
        if ch in '\n\t' or not (unicodedata.category(ch) in ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp') or ord(ch) in INVISIBLE_EXTRA):
            continue
        prev, nxt = (s[i - 1] if i else ''), (s[i + 1] if i + 1 < len(s) else '')
        if ord(ch) in EMOJI_SELECTORS and prev and _emoji(prev):
            continue
        if ch == '\u200d' and prev and nxt and (_emoji(prev) or ord(prev) in EMOJI_SELECTORS and i > 1 and _emoji(s[i - 2])) \
                and _emoji(nxt):
            continue
        if unicodedata.category(ch) == 'Cn' and 0x1F000 <= ord(ch) <= 0x1FAFF:
            continue  # an emoji newer than this Python's Unicode tables
        out.add('U+%04X' % ord(ch))
    return sorted(out)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def manifest(folder):
    out = {}
    for base, _, files in os.walk(folder):
        for name in files:
            if name.startswith('.oneiros-') and name.endswith('.tmp'):
                continue  # a temporary file of an atomic write in progress (or left by a crash)
            p = os.path.join(base, name)
            out[os.path.relpath(p, folder)] = sha(p)
    return out


# ---------- render ----------

TAG_RE = re.compile(r'<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|user-prompt-submit-hook)[^>]*>.*?</\1>', re.S)
# text the person did not type: pastes, IDE context and the output of shell commands run from the prompt
PASTE_TAGS = r'pasted_content|ide_[a-z_]+|bash-stdout|bash-stderr'
PASTE_ANY_RE = re.compile(r'</?(?:%s)\b[^>]*>' % PASTE_TAGS, re.I)  # an opening or closing paste tag
PASTE_CLOSE_RE = re.compile(r'</(?:%s)\b[^>]*>' % PASTE_TAGS, re.I)
SECRET_RE = re.compile(
    r'(?<![A-Za-z0-9])(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}|(?:sk|rk|pk)_live_[A-Za-z0-9]{16,}'
    r'|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|hf_[A-Za-z0-9]{30,})'
    r'|(?i:\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*)(?=[^\s\'"]*\d)(?=[^\s\'"]*[A-Za-z])[^\s\'"]{10,}'
    r'|(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}'
    r'|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|\b\d{8,10}:AA[A-Za-z0-9_-]{30,}'
    r'|hooks\.slack\.com/services/[A-Za-z0-9/]+|(?i:bearer)\s+[A-Za-z0-9._~+/-]{20,}=*'
    r'|(?<=://)[^\s/:@]+:[^\s/@]+(?=@)'
    r'|(?i:-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$))', re.S)
ISO_TS_RE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?')
TOOL_NAME_RE = re.compile(r'[A-Za-z0-9_.:-]{1,200}')
PASTE_SPAN_RE = re.compile(r'\[pasted: .*?(?: \.\.\.\]|$)')


def _text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get('type') == 'text':
            parts.append(b.get('text') or '')
    return '\n'.join(parts)


def _is_human(r):
    return r.get('turnOrigin') == 'human' or (r.get('origin') or {}).get('kind') == 'human'


def redact(text):
    """Credentials of well-known shapes are never sent to a model call or written anywhere."""
    return SECRET_RE.sub('[secret removed]', text or '')


def _clean_user(text):
    text = redact(TAG_RE.sub(' ', text))
    # a ']' inside pasted text would let it close the span early, so it is rendered as ')'
    def paste(body):
        return ' [pasted: %s ...] ' % squash(body)[:PASTE_CAP].replace(']', ')')
    # Pasted text can hold, or forge, closing and opening tags, so pairs cannot be trusted: from the first paste tag to
    # the last closing tag is pasted (the person's words between two pastes count as pasted too: fail closed).
    m = PASTE_ANY_RE.search(text)
    if m:
        closes = list(PASTE_CLOSE_RE.finditer(text, m.start()))
        end = closes[-1].end() if closes else len(text)
        text = text[:m.start()] + paste(PASTE_ANY_RE.sub(' ', text[m.start():end])) + text[end:]
    return squash(text)[:USER_LINE_CAP]


def _tool_line(b):
    inp = b.get('input') if isinstance(b.get('input'), dict) else {}
    hint = ''
    for k in ('description', 'command', 'file_path', 'pattern', 'url', 'query', 'prompt', 'skill'):
        if isinstance(inp.get(k), str) and inp[k].strip():
            hint = inp[k]
            break
    name = b.get('name') if isinstance(b.get('name'), str) and TOOL_NAME_RE.fullmatch(b.get('name')) else 'tool'
    return ('. %s: %s' % (name, squash(redact(hint))[:160])).rstrip(': ')


def render_session(path, cap=RENDER_CAP):
    """Transcript JSONL to text: '> ' human lines, '< ' assistant text, '. ' tool calls. Tool results, thinking and
    system reminders are never rendered."""
    sid = os.path.splitext(os.path.basename(path))[0]
    title, start, end = '', None, None
    lines = []  # (kind, text)
    with open(path, encoding='utf-8', errors='replace') as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            t = r.get('type')
            if t == 'custom-title':
                title = r.get('customTitle') if isinstance(r.get('customTitle'), str) else title
                continue
            ts = r.get('timestamp')
            if t in ('user', 'assistant') and isinstance(ts, str) and ISO_TS_RE.fullmatch(ts):
                start = start or ts
                end = ts
            msg = r.get('message') if isinstance(r.get('message'), dict) else {}
            if t == 'user' and r.get('isCompactSummary'):
                # a model-written summary of earlier turns: context, never the person's own words
                text = squash(redact(TAG_RE.sub(' ', _text_of(msg.get('content')))))
                if text:
                    lines.append(('assistant', '< [summary of earlier turns] ' + text[:ASSISTANT_LINE_CAP]))
                continue
            if t == 'user' and _is_human(r):
                text = _clean_user(_text_of(msg.get('content')))
                if text:
                    lines.append(('user', '> ' + text))
            elif t == 'user' and r.get('turnOrigin') == 'sdk' and not r.get('isMeta'):
                # a brief written by the session that started this one: context, never the person's own words
                text = _clean_user(_text_of(msg.get('content')))
                if text:
                    lines.append(('assistant', '< [brief from the starting session] ' + text))
            elif t == 'assistant':
                for b in (msg.get('content') if isinstance(msg.get('content'), list) else []):
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'text' and (b.get('text') or '').strip():
                        lines.append(('assistant', '< ' + squash(redact(b['text']))[:ASSISTANT_LINE_CAP]))
                    elif b.get('type') == 'tool_use':
                        lines.append(('tool', _tool_line(b)))
    header = '# session %s | %s | %s to %s' % (sid, clean(redact(title), 120) or 'untitled', start or '?', end or '?')
    n_user = sum(1 for k, _ in lines if k == 'user')
    budget = cap - len(header) - 1 - sum(len(x) + 1 for k, x in lines if k == 'user')
    kept, dropped = [], 0
    for k, x in lines:
        if k == 'user':
            kept.append(x)
        elif budget - (len(x) + 1) >= 0:
            kept.append(x)
            budget -= len(x) + 1
        else:
            dropped += 1
    if dropped:
        kept.append('[... %d assistant or tool lines dropped at the %d character cap]' % (dropped, cap))
    text = header + '\n' + '\n'.join(kept) + '\n'
    return text, {'sid': sid, 'title': title, 'start': start, 'end': end, 'n_user': n_user, 'chars': len(text)}


def session_index(project=PROJECT):
    """All transcripts of the project: sid, start, title, human turn count (cheap byte scan), sorted by start then sid."""
    out = []
    for name in os.listdir(project):
        if not name.endswith('.jsonl') or os.path.islink(os.path.join(project, name)):
            continue
        p = os.path.join(project, name)
        start, human, title = None, 0, ''
        with open(p, 'rb') as f:
            for raw in f:
                if start is None and b'"timestamp"' in raw and (b'"type":"user"' in raw or b'"type":"assistant"' in raw):
                    try:
                        ts = json.loads(raw).get('timestamp')
                        start = ts if isinstance(ts, str) and ISO_TS_RE.fullmatch(ts) else None
                    except (ValueError, AttributeError):
                        pass
                if b'"type":"user"' in raw and (b'"turnOrigin":"human"' in raw or b'"kind":"human"' in raw) \
                        and b'"isCompactSummary":true' not in raw:
                    human += 1
                if b'"custom-title"' in raw:
                    try:
                        ct = json.loads(raw).get('customTitle')
                        title = ct if isinstance(ct, str) else title
                    except (ValueError, AttributeError):
                        pass
        out.append({'sid': name[:-6], 'path': p, 'start': start, 'n_user': human, 'title': title,
                    'bytes': os.path.getsize(p), 'mtime': os.path.getmtime(p)})
    out.sort(key=lambda s: (s['start'] or '', s['sid']))
    return out


# ---------- verify candidates ----------

def quote_role(render_text, quote):
    """Role of the first render line that holds the quote (whitespace collapsed): 'user', 'assistant', 'tool', or
    'pasted' when every occurrence in that user line touches a [pasted: ...] span. None when no line holds it."""
    q = squash(quote)
    if not q:
        return None
    for line in render_text.splitlines():
        kind = {'> ': 'user', '< ': 'assistant', '. ': 'tool'}.get(line[:2])
        if kind is None:
            continue
        body = squash(line[2:])
        pos = body.find(q)
        if pos < 0:
            continue
        if kind != 'user':
            return kind
        spans = [m.span() for m in PASTE_SPAN_RE.finditer(body)]
        while pos >= 0:
            if not any(pos < e and pos + len(q) > s for s, e in spans):
                return 'user'
            pos = body.find(q, pos + 1)
        return 'pasted'
    return None


def quote_line(render_text, quote):
    """Body (prefix removed, whitespace collapsed) of the first render line that holds the quote, else None."""
    q = squash(quote)
    if q:
        for line in render_text.splitlines():
            if line[:2] in ('> ', '< ', '. ') and q in squash(line[2:]):
                return squash(line[2:])
    return None


def typed_lines(render_text, quote, radius=GROUND_RADIUS):
    """The person's own render lines ('> ', pasted spans removed) within `radius` lines of the quote."""
    q = squash(quote)
    lines = [(l[:2], squash(l[2:])) for l in render_text.splitlines() if l[:2] in ('> ', '< ', '. ')]
    for i, (_, b) in enumerate(lines):
        if q and q in b:
            return [PASTE_SPAN_RE.sub(' ', x) for k, x in lines[max(0, i - radius):i + radius + 1] if k == '> ']
    return []


def grounding_lines(render_text, quote, role, radius=GROUND_RADIUS):
    """Texts that may ground the numbers of a statement: the render line that holds the quote and the render lines
    (prefix '> ', '< ' or '. ') up to `radius` before and after it. Text inside a [pasted: ...] span is left out
    unless the candidate's role is 'pasted'. Empty list when no line holds the quote."""
    q = squash(quote)
    body = [squash(l[2:]) for l in render_text.splitlines() if l[:2] in ('> ', '< ', '. ')]
    if not q:
        return []
    for i, b in enumerate(body):
        if q in b:
            window = body[max(0, i - radius):i + radius + 1]
            return window if role == 'pasted' else [PASTE_SPAN_RE.sub(' ', w) for w in window]
    return []


def _session_days(render):
    head = render.split('\n', 1)[0]
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})\S* to (\d{4}-\d{2}-\d{2})\S*', head.rsplit(' | ', 1)[-1])
    try:
        return (dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2))) if m and head.startswith('# session') else None
    except ValueError:
        return None


def _day_in(token, days):
    """token is 'YYYY-MM-DD' or 'MM-DD' (as unsupported_numbers prints a date); is it inside the session's days?"""
    if not days:
        return False
    parts = [int(x) for x in token.split('-')]
    try:
        if len(parts) == 3:
            return days[0] <= dt.date(*parts) <= days[1]
        return any(days[0] <= dt.date(y, *parts) <= days[1] for y in {days[0].year, days[1].year})
    except ValueError:
        return False


def ungrounded(text, lines, days):
    """Numbers and dates of `text` that none of the grounding `lines` (a text or a list of texts) nor the session's
    days carry."""
    lines = [lines or ''] if isinstance(lines, str) or lines is None else list(lines)
    return [x for x in unsupported_numbers([text], lines)
            if not (re.fullmatch(r'(?:\d{4}-)?\d{2}-\d{2}', x) and _day_in(x, days))]


def verify_candidates(render, candidates, sid=None):
    """Keep a candidate only if its quote (whitespace collapsed, at least MIN_QUOTE characters) sits inside one
    render line of the stated role. A quote inside a pasted span gets the role 'pasted' whatever the model claimed.
    The numbers of the statement must also stand on that line or within GROUND_RADIUS render lines of it (dates may
    instead fall inside the session's days), so a model cannot launder a number through its own paraphrase; pasted
    text grounds only a candidate whose role is 'pasted'. A `date` outside the session's days is cleared.
    Kept candidates get the id 'c<sid8>-<n>' (n counts the kept ones of this session)."""
    if sid is None:
        m = re.match(r'# session (\S+)', render)
        sid = m.group(1) if m else '00000000'
    days = _session_days(render)
    kept, dropped = [], []
    for c in candidates:
        q, claimed = squash(c.get('quote')), c.get('role')
        found = quote_role(render, q) if len(q) >= MIN_QUOTE else None
        if len(q) < MIN_QUOTE:
            dropped.append(dict(c, why='quote too short'))
        elif claimed not in ('user', 'assistant', 'pasted'):
            dropped.append(dict(c, why='unknown role'))
        elif found not in ('user', 'assistant', 'pasted'):
            dropped.append(dict(c, why='quote not found on a %s line' % claimed))
        elif found != 'pasted' and found != claimed:
            dropped.append(dict(c, why='quote not found on a %s line' % claimed))
        else:
            if any(isinstance(v, str) and SECRET_RE.search(v) for v in c.values()):
                dropped.append(dict(c, why='a field holds a credential'))
                continue
            odd = [k for k in ('date', 'kind', 'target') if isinstance(c.get(k), str) and strong_tokens(c[k])]
            if odd or (isinstance(c.get('target'), str) and c['target'] != 'new' and not FILE_RE.match(c['target'])):
                dropped.append(dict(c, why='field %s holds a link or code, or the target is not a memory file name'
                                    % ', '.join(odd or ['target'])))
                continue
            line = grounding_lines(render, q, found)
            bad = ungrounded(str(c.get('statement') or ''), line, days)
            if bad:
                dropped.append(dict(c, why='statement numbers not within %d lines of the quote: %s' % (GROUND_RADIUS, ', '.join(bad))))
                continue
            support = typed_lines(render, q) if found == 'user' else ([line] if isinstance(line, str) else list(line))
            strings = unsupported_strings([str(c.get('statement') or '')], support)
            if strings:
                dropped.append(dict(c, why='statement links, addresses or code not within %d lines of the quote: %s'
                                    % (GROUND_RADIUS, ', '.join(strings))))
                continue
            date = c.get('date') or ''
            if date and (not isinstance(date, str) or ungrounded(date, line, days)):
                c = dict(c, date='')
            kept.append(dict(c, role=found, session=sid, id='c%s-%d' % (sid[:8], len(kept) + 1)))
    return kept, dropped


# ---------- fact tokens and matching ----------

def fact_tokens(text):
    """Numbers (thousands separators removed), letter-digit ids and upper-case identifiers, original case."""
    text = text or ''
    toks = set()
    for m in re.findall(r'(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?', text):
        toks.add(m.rstrip('.,').replace(',', ''))
    toks.update(re.findall(r'\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]+\b', text))
    toks.update(re.findall(r'\b[A-Z][A-Z0-9]+\b', text))
    toks.discard('')
    return toks


def _norm_tokens(text):
    text = re.sub(r'(?<=\d),(?=\d{3}\b)', '', (text or '').lower())
    return re.findall(r'[a-z0-9]+(?:\.[0-9]+)?', text)


def exact_match(gold, answer):
    """Every token of a short gold answer appears as a whole token in the answer (case, punctuation and thousands
    separators ignored)."""
    g, a = _norm_tokens(gold), set(_norm_tokens(answer))
    return bool(g) and all(t in a for t in g)


# ---------- numbers and dates in written text ----------

MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10,
          'nov': 11, 'dec': 12}
_MON = r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
ISO_DATE_RE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')
DMY_RE = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+' + _MON + r'\b\.?(?:,?\s+(\d{4})\b)?', re.I)
MDY_RE = re.compile(r'\b' + _MON + r'\b\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(\d{4})\b)?', re.I)
MY_RE = re.compile(r'\b' + _MON + r'\b\.?,?\s+(\d{4})\b', re.I)
NUM_RE = re.compile(r'\d+(?:[.,]\d+)*')
ORDINALS = ('st', 'nd', 'rd', 'th')
# number words that carry an amount; one to ten are left out because they are ordinary prose ("one more reason")
NUMBER_WORDS = frozenset('eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty '
                         'fifty sixty seventy eighty ninety hundred thousand million billion trillion dozen'.split())


def _month(word):
    if word.lower() == 'may' and word not in ('May', 'MAY'):
        return None  # the modal verb
    return MONTHS[word[:3].lower()]


def _plain_number(part):
    m = re.fullmatch(r'(\d+)(?:\.(\d+))?', part)
    if not m:
        return part
    frac = (m.group(2) or '').rstrip('0')
    return (m.group(1).lstrip('0') or '0') + ('.' + frac if frac else '')


def _ascii_digits(text):
    """Digits of any script (fullwidth, Arabic-Indic and so on) become ASCII digits."""
    if text.isascii():
        return text
    return ''.join(str(unicodedata.decimal(c, -1)) if unicodedata.decimal(c, -1) >= 0 else c for c in text)


def _number_form(n):
    """One numeral with thousands separators and leading or trailing zeros normalised."""
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', n):
        n = n.replace(',', '')
    return _plain_number(n)


def number_tokens(text):
    """(plain tokens, dates) of a text. Plain tokens are numerals (thousands separators and leading zeros normalised),
    numerals glued to letters (4M, EUR4m, 2026Q4, TP1234, v3) kept whole in lower case, and the amount words in
    NUMBER_WORDS. Month names become numbers; dates are (month, day, year or None). Digits of any script count as
    ASCII digits."""
    text = _ascii_digits(text or '')
    dates, plain = set(), set()

    def date(m, order):
        if order == 'iso':
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        elif order == 'dmy':
            d, mo, y = int(m.group(1)), _month(m.group(2)), int(m.group(3) or 0) or None
        else:
            mo, d, y = _month(m.group(1)), int(m.group(2)), int(m.group(3) or 0) or None
        if not mo:
            return m.group(0)
        dates.add((mo, d, y))
        return ' '

    def month_year(m):
        mo = _month(m.group(1))
        if not mo:
            return m.group(0)
        plain.update((str(mo), str(int(m.group(2)))))
        return ' '

    text = ISO_DATE_RE.sub(lambda m: date(m, 'iso'), text)
    text = DMY_RE.sub(lambda m: date(m, 'dmy'), text)
    text = MDY_RE.sub(lambda m: date(m, 'mdy'), text)
    text = MY_RE.sub(month_year, text)
    for m in NUM_RE.finditer(text):
        s, e = m.span()
        i, j = s, e
        while i > 0 and text[i - 1].isalpha():
            i -= 1
        while j < len(text) and text[j].isalpha():
            j += 1
        left, right = text[i:s], text[e:j]
        if (left or right) and not (not left and right.lower() in ORDINALS):
            plain.add((left + _number_form(m.group(0)) + right).lower())
            continue
        n = m.group(0)
        if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', n):
            n = n.replace(',', '')
        for part in n.split(','):
            if part:
                plain.add(_plain_number(part))
    plain.update(w for w in re.findall(r'[a-z]+', text.lower()) if w in NUMBER_WORDS)
    return plain, dates


def unsupported_numbers(written, support, extra=''):
    """Numbers, numerals glued to letters, amount words and dates of the `written` texts that appear in none of the
    `support` texts (or in `extra`)."""
    plain, dates = set(), set()
    for t in list(support) + [extra]:
        p, d = number_tokens(t)
        plain |= p
        dates |= d
    for mo, day, y in dates:
        plain.update((str(mo), str(day)) + ((str(y),) if y else ()))
    bad = []
    for t in written:
        p, d = number_tokens(t)
        bad += sorted(x for x in p if x not in plain)
        for mo, day, y in sorted(d, key=lambda x: (x[0], x[1], x[2] or 0)):
            if not any(mo == m2 and day == d2 and (y is None or y2 is None or y == y2) for m2, d2, y2 in dates):
                bad.append('%s%02d-%02d' % ('%d-' % y if y else '', mo, day))
    return sorted(set(bad))


# ---------- memory index ----------

URL_RE = re.compile(r'\b[a-z][a-z0-9+.-]{1,15}://\S+|(?<![\w:/])//[\w-]+(?:\.[\w-]+)+\S*', re.I)  # a whole non-space run
SCHEME_RE = re.compile(r'\b(?:javascript|data|vbscript|file|tel|sms|mailto|intent):\S+', re.I)
TLDS = ('com|net|org|io|ai|dev|app|co|uk|de|fr|gr|eu|info|biz|xyz|me|us|ru|cn|jp|nl|it|es|ch|at|be|se|no|dk|fi|pl|cz|pt|ie|'
        'ca|au|nz|in|br|mx|za|kr|tw|hk|sg|il|tr|ua|top|club|shop|store|live|news|blog|page|link|click|icu|vip|online|site|'
        'cloud|tech|ly|gg|tv|ws|cc|so|to|su|onion|local|localhost|tk|ml|ga|cf|gq|work|space|run|lol|pw|fm|im|gl|is|lu|ro|'
        'zip|mov|cyou|rest|bar|buzz|monster|quest|sbs|cfd|\u0440\u0444')
HOST_PATH_RE = re.compile(r'(?<![\w@./-])(?:[\w-]+\.)+[A-Za-z]{2,24}(?::\d+)?/\S*'  # any host followed by a path
                          r'|(?<![\w@./-])(?:[\w-]+\.)+(?:%s)\b(?::\d+)?(?:/\S*)?|(?<![\w@./-])localhost(?::\d+)?(?:/\S*)?'
                          r'|(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/\S*)?|\[[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*\](?::\d+)?(?:/\S*)?'
                          % TLDS, re.I)
TAG_CODE_RE = re.compile(r'<(code|script|pre)\b[^>]*>([\s\S]*?)(?:</\1\s*>|$)', re.I)
DOTS = {0x3002: '.', 0xFF0E: '.', 0xFF61: '.'}
EMAIL_RE = re.compile(r'\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b')
WWW_RE = re.compile(r'\bwww\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', re.I)
MD_DEST_RE = re.compile(r'\]\(\s*<?([^)\s>]+)')
FENCE_RE = re.compile(r'(```|~~~)([\s\S]*?)(?:\1|$)')
CODE_RE = re.compile(r'`([^`\n]{3,})`')


def _trim(tok):
    """A link without trailing punctuation, wrapper characters and closing brackets it did not open."""
    while True:
        before = tok
        tok = tok.rstrip('.,;:!?*"\'`')
        for o, c in (('(', ')'), ('[', ']'), ('{', '}'), ('<', '>')):
            if tok.endswith(c) and tok.count(c) > tok.count(o):
                tok = tok[:-1]
        if tok == before:
            return tok


def strong_tokens(text):
    """Strings a dream must never invent: links of any scheme or shape (including markdown destinations other than
    a bare memory file name), e-mail addresses, www host names, fenced and backtick code. Compared after NFKC, so
    full-width look-alikes count as what they look like."""
    text = strip_invisible(unicodedata.normalize('NFKC', text or '').translate(DOTS))
    text = re.sub(r'\[\.\]|\(\.\)|\[dot\]|\(dot\)', '.', text, flags=re.I)  # defanged dots count as dots
    toks = {_trim(u) for u in URL_RE.findall(text) + HOST_PATH_RE.findall(text) + SCHEME_RE.findall(text)}
    toks |= {d for d in MD_DEST_RE.findall(text) if not FILE_RE.match(d)}
    toks |= set(EMAIL_RE.findall(text)) | set(WWW_RE.findall(text))
    toks |= {squash(c) for _, c in FENCE_RE.findall(text) if squash(c)}
    toks |= {squash(c) for _, c in TAG_CODE_RE.findall(text) if squash(c)}
    toks |= {squash(c) for c in CODE_RE.findall(FENCE_RE.sub(' ', text)) if squash(c)}
    return {t for t in toks if t}


def unsupported_strings(written, support, extra=''):
    """Strong tokens of the `written` texts that do not stand as a whole token in any `support` text (or `extra`):
    whitespace collapsed and case folded, but a longer address or path does not support a shorter or different one."""
    have = squash(strip_invisible(unicodedata.normalize('NFKC', ' '.join(list(support) + [extra or ''])).translate(DOTS))).casefold()
    def present(tok):
        return re.search(r'(?<![\w@.-])%s(?![\w@-])' % re.escape(squash(tok).casefold()), have) is not None
    return sorted({tok for w in written for tok in strong_tokens(w) if not present(tok)})


def index_path(mem):
    return os.path.join(mem, 'MEMORY.md')


def index_find(lines, file):
    for i, line in enumerate(lines):
        if '](%s)' % file in line:
            return i
    return None


def frontmatter_end(text):
    if not text.startswith('---\n'):
        return 0
    j = text.find('\n---', 4)
    return j + 4 if j >= 0 else 0


# ---------- apply operations ----------

def _check_file(name, mem, must_exist=True):
    if not isinstance(name, str) or '/' in name or '\\' in name or name.startswith('.'):
        return 'file must be a bare name in the memory folder'
    if name.lower() == 'memory.md':
        return 'MEMORY.md changes only through set_index_line'
    if not FILE_RE.match(name):
        return 'file must be a lower-case .md name'
    exists = os.path.isfile(os.path.join(mem, name))
    if must_exist and not exists:
        return 'no such memory file'
    return None


def _resolve_sources(op, sessions):
    src = op.get('sources')
    if not isinstance(src, list) or not src:
        return None, 'sources must list at least one session of this run'
    full = []
    for s in src:
        hits = [x for x in sessions if isinstance(s, str) and len(s) >= 8 and x.startswith(s)]
        if len(hits) != 1:
            return None, 'a source is not a session of this run'
        full.append(hits[0])
    return full, None


def _has_dash(*texts):
    return any(d in (t or '') for t in texts for d in DASHES)


def _dash_field(op):
    """Name of the first string field of an op that holds an em or en dash (`old` quotes existing text and an index
    line is checked by _valid_index_line), else None."""
    for k, v in op.items():
        if k == 'old' or (k == 'line' and op.get('op') == 'set_index_line'):
            continue
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, str) and _has_dash(x):
                return k
    return None


def _iso_day(s):
    try:
        return bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', s)) and bool(dt.date.fromisoformat(s))
    except ValueError:
        return False


def _file_type(text):
    m = re.search(r'^\s*type:\s*[\"\']?([A-Za-z]+)', text[:frontmatter_end(text)], re.M)
    return m.group(1).lower() if m else None


def _cited(op, candidates, srcs):
    """Kept candidates that the op cites by id and whose session is one of the op's sources."""
    ids = op.get('cands')
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        return []
    return [c for c in candidates if c.get('id') in ids and c.get('session') in (srcs or [])]


def _tag(today, sources, role=None):
    return '(dream %s: session %s%s)' % (today, ', '.join(s[:8] for s in sources), ', ' + role if role else '')


def _bare_links(line):
    """Memory file names an index line links to (bare names only: a path or URL is not a memory file)."""
    return [f for f in LINK_RE.findall(line) if FILE_RE.match(f)]


def _valid_index_line(line, file, new):
    if not isinstance(line, str) or any(c in line for c in '\n\r\x0b\x0c\x85\u2028\u2029'):
        return 'index line must be one line'
    if any(not FILE_RE.match(t) for t in re.findall(r'\]\(([^)]*)\)', line)):
        return 'every link in an index line must be a bare memory file name'
    m = INDEX_LINE_RE.match(line)
    if not m or m.group(1) != file:
        return 'index line must start "- [Title](%s)"' % file
    cap = NEW_INDEX_LINE_CAP if new else INDEX_LINE_CAP
    if len(line) > cap:
        return 'index line is %d characters, cap %d' % (len(line), cap)
    rest = line[m.end():]
    if _has_dash(line[:m.end()], rest[3:] if rest.startswith(' — ') else rest):
        return 'em or en dash in the index line (only the separator after the link may be one)'
    return None


def _guard_hook(line):
    """An index line with every link target blanked, apostrophe variants made straight and double-quote variants made
    one straight quote."""
    t = re.sub(r'\]\([^)]*\)', '](x)', line or '')
    t = re.sub('[‘’ʼ`´]', "'", t)
    return re.sub('[“”„«»]', '"', t)


def _word_seq(text):
    """Case-folded word sequence, space-joined and padded, for whole-word containment tests."""
    return ' %s ' % ' '.join(re.findall(r'\w+', (text or '').casefold()))


def _guard_parts(line):
    """({label: count}, quoted spans, status words) of an index line. Counts follow NEG_RE and GUARD_RES on the
    case-folded hook; a 'not' that starts a status phrase (STATUS_TAIL_RE, a deny-listed word only after an adverb) is
    not counted and its status word is listed instead. Spans are the text between paired double quotes; when the quote
    marks are unbalanced, every fragment between neighbouring marks is kept instead (fail closed: a stray mark cannot
    hide a quoted correction)."""
    hook = _guard_hook(line)
    low = hook.casefold()
    counts, status = {}, []
    for m in NEG_RE.finditer(low):
        if m.group('not'):
            tail = STATUS_TAIL_RE.match(low, m.end())
            if tail and (tail.group('adv') or not re.match(_DENY, tail.group('word'))):
                status.append(tail.group('word'))
                continue
        label = NEVER if m.group('never') else NOT
        counts[label] = counts.get(label, 0) + 1
    for label, rx in GUARD_RES:
        n = len(rx.findall(low))
        if n:
            counts[label] = n
    marks = [i for i, ch in enumerate(hook) if ch == '"']
    pairs = zip(marks[0::2], marks[1::2]) if len(marks) % 2 == 0 else zip(marks, marks[1:])
    spans = [hook[a + 1:b] for a, b in pairs]
    return counts, [s for s in spans if _word_seq(s).strip()], status


def guard_terms(line):
    """Guard wording of an index line: ({label: count}, quoted spans). See _guard_parts."""
    counts, spans, _ = _guard_parts(line)
    return counts, spans


def lost_guard_terms(old, new):
    """Guard wording of index line `old` that `new` drops: a label counted fewer times, a status word after 'not' that
    no longer appears as a whole word as often (reported as 'not <word>'), or a quoted span whose words no longer appear
    in order as whole words (case, punctuation and quote marks ignored). [] when nothing is lost. A shortened index line
    may lose detail its topic file keeps, but not a user correction, warning or prohibition: the index is what every
    session loads. A counting tripwire, not a reading of meaning: a rewrite that keeps the counts passes even if it
    changes what they say ('not shared' to 'shared' keeps the word 'shared')."""
    co, so, st = _guard_parts(old)
    cn, _, _ = _guard_parts(new)
    words = _word_seq(_guard_hook(new))
    tokens = words.split()
    lost = [k for k in sorted(co) if cn.get(k, 0) < co[k]]
    lost += ['not %s' % w for w in dict.fromkeys(st) if tokens.count(w) < st.count(w)]
    lost += ['"%s"' % s for s in dict.fromkeys(so) if _word_seq(s) not in words]
    return lost


def added_guard_terms(old, new, topic):
    """Guard labels (and quoted spans) that index line `new` holds more of than `old`, which its linked topic text does
    not hold either: an index line, read by every session, may not introduce a rule or warning of its own."""
    co, so = guard_terms(old)
    cn, sn = guard_terms(new)
    ct, _ = guard_terms(topic)
    words = _word_seq(topic)
    out = [k for k in sorted(cn) if cn[k] > co.get(k, 0) and k not in ct]
    out += ['"%s"' % s for s in dict.fromkeys(sn) if s not in so and _word_seq(s) not in words]
    return out


def _first_link(line):
    m = INDEX_LINE_RE.match(line) or LINK_RE.search(line)
    return m.group(1) if m else None


def index_line_successors(old_lines, new_lines):
    """For each line of the old index that links a memory file and is gone from the new index: (file, old line, the line
    that replaced it or None). Lines are aligned in order (difflib), so a changed line is paired within its changed
    block with the line that has the same first link; a line moved elsewhere is paired with a new or changed line that
    starts with, else links, that file."""
    keep, olds = set(new_lines), set(old_lines)
    fresh = [n for n in new_lines if n not in olds]
    fresh_set = set(fresh)
    out = []
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ('equal', 'insert'):
            continue
        block, used = new_lines[j1:j2], set()
        for old in old_lines[i1:i2]:
            key = _first_link(old)
            if not key or old in keep:
                continue
            succ = None
            for k, n in enumerate(block):
                if k not in used and n in fresh_set and _first_link(n) == key:
                    succ = n
                    used.add(k)
                    break
            if succ is None:
                succ = next((n for n in fresh if _first_link(n) == key), None) or \
                    next((n for n in fresh if '](%s)' % key in n), None)
            out.append((key, old, succ))
    return out


def apply_ops(mem, ops, sessions, today, candidates=None):
    """Apply discrete operations to a memory folder. Returns (applied ops, [{'op', 'reason'}]). With `candidates` (the
    kept candidate dicts of the run) every writing op must cite them by id (provenance, role and number checks)."""
    applied, rejected = [], []
    planned_index = {o.get('file') for o in ops if isinstance(o, dict) and o.get('op') == 'set_index_line'
                     and isinstance(o.get('file'), str)}
    # index lines go last, so an add_file is on disk before the line that links it
    ops = [o for o in ops if not (isinstance(o, dict) and o.get('op') == 'set_index_line')] + \
          [o for o in ops if isinstance(o, dict) and o.get('op') == 'set_index_line']

    def reject(op, why):
        rejected.append({'op': op, 'reason': why})

    seen = set()
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get('op'), str) or op['op'] not in OPS:
            reject(op, 'unknown operation')
            continue
        key = json.dumps(op, sort_keys=True, default=str)
        if key in seen:
            reject(op, 'duplicate of an earlier operation in the list')
            continue
        seen.add(key)
        wrong = [k for k in STR_FIELDS if k in op and not isinstance(op[k], str)]
        if wrong:
            reject(op, 'field %s must be a string' % wrong[0])
            continue
        kind, name = op['op'], op.get('file')
        srcs, why = _resolve_sources(op, sessions)
        if why and kind != 'archive':
            reject(op, why)
            continue
        if kind == 'archive' and why and not (op.get('reason') or '').strip():
            reject(op, 'archive needs a reason')
            continue
        why = _check_file(name, mem, must_exist=(kind != 'add_file'))
        if why:
            reject(op, why)
            continue
        path = os.path.join(mem, name)
        if os.path.islink(path) or os.path.islink(index_path(mem)) or os.path.islink(os.path.join(mem, 'archive')):
            reject(op, 'the target, MEMORY.md or archive/ is a symlink; ONEIROS never writes through links')
            continue
        if any(isinstance(v, str) and SECRET_RE.search(v) for k, v in op.items() if k != 'old'):
            reject(op, 'holds a credential; credentials are never written to memory')
            continue
        size = {'append': len(squash(op.get('text'))), 'conflict': len(squash(op.get('text'))),
                'supersede': len(op.get('old') or '') + len(squash(op.get('new'))),
                'add_file': sum(len(op.get(k) or '') for k in ('name', 'description', 'body'))}.get(kind, 0)
        if size > MAX_OP_CHARS:
            reject(op, 'writes %d characters, over %d; split it into several operations' % (size, MAX_OP_CHARS))
            continue
        hidden = sorted({h for k in ('text', 'new', 'name', 'description', 'body', 'line', 'reason')
                         if isinstance(op.get(k), str) for h in hidden_chars(op[k])})
        if hidden:
            reject(op, 'holds characters a reader cannot see (%s)' % ', '.join(hidden[:5]))
            continue
        if (os.path.exists(path) and not os.access(path, os.W_OK)) or (kind in ('set_index_line', 'archive') and
                os.path.exists(index_path(mem)) and not os.access(index_path(mem), os.W_OK)):
            reject(op, 'the target or MEMORY.md is read-only; ONEIROS does not change locked files')
            continue
        if name in PROTECTED and kind != 'append':
            reject(op, 'protected file: append only')
            continue
        field = _dash_field(op)
        if field:
            reject(op, 'em or en dash in field %s' % field)
            continue
        role = op.get('role')
        if candidates is not None and kind in ('add_file', 'append', 'supersede', 'conflict'):
            cited = _cited(op, candidates, srcs)
            if not cited:
                reject(op, 'cands must cite at least one kept candidate of a cited session')
                continue
            target = op.get('type') if kind == 'add_file' else _file_type(read(path))
            if (target in ('user', 'feedback') or (kind != 'add_file' and target is None)) \
                    and not all(c.get('role') == 'user' for c in cited):
                reject(op, 'a %s file needs every cited candidate to have role user' % (target or 'file without a type'))
                continue
            written = {'append': ['text'], 'conflict': ['text'], 'supersede': ['new', 'date'],
                       'add_file': ['name', 'description', 'body']}[kind]
            bad = unsupported_numbers([op.get(k) for k in written if isinstance(op.get(k), str)],
                                      [c.get(k) or '' for c in cited for k in ('statement', 'quote', 'date')],
                                      today + '\n' + (read(path) if kind != 'add_file' else ''))
            if bad:
                reject(op, 'numbers not in the cited candidates or the target file: %s' % ', '.join(bad))
                continue
            strings = unsupported_strings([op.get(k) for k in written if isinstance(op.get(k), str)],
                                          [c.get(k) or '' for c in cited for k in ('statement', 'quote')],
                                          read(path) if kind != 'add_file' else '')
            if strings:
                reject(op, 'links, addresses or code not in the cited candidates or the target file: %s' % ', '.join(strings))
                continue
            role = '/'.join(sorted({c.get('role') for c in cited}))
        if candidates is not None and kind == 'archive':
            target = _file_type(read(path))
            cited = _cited(op, candidates, srcs)
            if (target in ('user', 'feedback') or target is None) and (not cited or not all(c.get('role') == 'user' for c in cited)):
                reject(op, 'archiving a %s file needs cited candidates that all have role user' % (target or 'file without a type'))
                continue
        try:
            if kind == 'append':
                text = squash(op.get('text'))
                if len(text) < 3 or _has_dash(text):
                    reject(op, 'text missing or holds an em or en dash')
                    continue
                body = read(path).rstrip('\n')
                if text in squash(body):
                    reject(op, 'text already in the file')
                    continue
                write(path, body + '\n\n' + text + ' ' + _tag(today, srcs, role) + '\n')
            elif kind == 'conflict':
                text = squash(op.get('text'))
                if len(text) < 3 or _has_dash(text):
                    reject(op, 'text missing or holds an em or en dash')
                    continue
                body = read(path).rstrip('\n')
                if text in squash(body):
                    reject(op, 'text already in the file')
                    continue
                write(path, body + '\n\nCONFLICT (%s): %s %s\n' % (today, text, _tag(today, srcs, role)))
            elif kind == 'supersede':
                old, new, date = op.get('old') or '', squash(op.get('new')), op.get('date') or today
                if not new or _has_dash(new, date):
                    reject(op, 'new text missing or holds an em or en dash')
                    continue
                if not _iso_day(date):
                    reject(op, 'date must be a calendar date YYYY-MM-DD')
                    continue
                text = read(path)
                head = frontmatter_end(text)
                if len(old) < MIN_QUOTE or text.count(old) != 1 or text.find(old) < head:
                    reject(op, 'old text must occur exactly once, outside the frontmatter')
                    continue
                text = text.replace(old, new, 1).rstrip('\n')
                write(path, text + '\n\nHistory: on %s "%s" was superseded %s\n' % (date, squash(old), _tag(today, srcs, role)))
            elif kind == 'add_file':
                if os.path.exists(path):
                    reject(op, 'file already exists')
                    continue
                if name not in planned_index:
                    reject(op, 'add_file needs a set_index_line for the same file')
                    continue
                idx = next(o for o in ops if isinstance(o, dict) and o.get('op') == 'set_index_line' and o.get('file') == name)
                bad = _valid_index_line(idx.get('line'), name, new=True)
                if bad:
                    reject(op, 'its index line is invalid: ' + bad)
                    continue
                fields = [op.get('name'), op.get('description'), op.get('body')]
                if op.get('type') not in TYPES or not all(isinstance(x, str) and x.strip() for x in fields):
                    reject(op, 'add_file needs name, description, body and a type in %s' % (TYPES,))
                    continue
                if _has_dash(*fields):
                    reject(op, 'em or en dash in the new file')
                    continue
                write(path, '---\nname: %s\ndescription: "%s"\nmetadata:\n  type: %s\n---\n\n%s %s\n' % (
                    squash(op['name']), squash(op['description']).replace('"', "'"), op['type'],
                    op['body'].strip(), _tag(today, srcs, role)))
            elif kind == 'archive':
                if not op.get('reason'):
                    reject(op, 'archive needs a reason')
                    continue
                ipath = index_path(mem)
                lines = read(ipath).split('\n')
                i = index_find(lines, name)
                if i is not None:
                    others = [f for f in _bare_links(lines[i]) if f != name]
                    if others:
                        reject(op, 'its index line also links %s; rewrite that line first' % ', '.join(others))
                        continue
                    del lines[i]
                os.makedirs(os.path.join(mem, 'archive'), exist_ok=True)
                dest = os.path.join(mem, 'archive', name)
                if os.path.exists(dest):
                    reject(op, 'archive already holds a file of that name')
                    continue
                shutil.move(path, dest)
                write(ipath, '\n'.join(lines))
            elif kind == 'set_index_line':
                ipath = index_path(mem)
                lines = read(ipath).split('\n')
                i = index_find(lines, name)
                bad = _valid_index_line(op.get('line'), name, new=(i is None))
                if bad:
                    reject(op, bad)
                    continue
                if not os.path.isfile(path):
                    reject(op, 'linked file does not exist (add_file it first)')
                    continue
                line = op['line']
                linked_text = '\n'.join(read(os.path.join(mem, f)) for f in _bare_links(line)
                                        if os.path.isfile(os.path.join(mem, f)))
                bad = unsupported_numbers([line], [linked_text])
                if bad:
                    reject(op, 'index line numbers not in the linked topic file: %s' % ', '.join(bad))
                    continue
                strings = unsupported_strings([line], [linked_text])
                if strings:
                    reject(op, 'index line links, addresses or code not in the linked topic file: %s' % ', '.join(strings))
                    continue
                if i is not None:
                    old = lines[i]
                    lost_links = [f for f in _bare_links(old) if f not in _bare_links(line)
                                  and index_find(lines[:i] + lines[i + 1:], f) is None]
                    if lost_links:
                        reject(op, 'the new line drops the only index link to %s' % ', '.join(lost_links))
                        continue
                    linked = [os.path.join(mem, f) for f in _bare_links(old) if os.path.isfile(os.path.join(mem, f))]
                    keep = {t.lower() for p in linked for t in fact_tokens(read(p))} | {t.lower() for t in fact_tokens(line)}
                    hook = LINK_RE.sub('](x)', old)
                    lost = sorted(t for t in fact_tokens(hook) if t.lower() not in keep and t.lower() != 'x')
                    if lost:
                        reject(op, 'facts only in the old index line would be lost: %s' % ', '.join(lost))
                        continue
                    added = added_guard_terms(old, line, linked_text)
                    if added:
                        reject(op, 'the new line adds rule or warning wording its topic file does not hold: %s' % ', '.join(added))
                        continue
                    dropped = lost_guard_terms(old, line)
                    if dropped:
                        reject(op, 'the new line drops warning or correction wording of the line it replaces: %s'
                               % ', '.join(dropped))
                        continue
                    lines[i] = line
                else:
                    while lines and lines[-1] == '':
                        lines.pop()
                    lines.append(line)
                    lines.append('')
                write(ipath, '\n'.join(lines))
        except OSError as e:
            reject(op, 'io error: %s' % e)
            continue
        applied.append(op)
    ipath = index_path(mem)
    text = read(ipath) if os.path.isfile(ipath) else ''
    for op in [o for o in applied if o['op'] == 'add_file']:
        if '](%s)' % op['file'] not in text:  # its set_index_line was rejected: no orphan file may stay
            os.remove(os.path.join(mem, op['file']))
            gone = [o for o in applied if o.get('file') == op['file']]
            applied = [o for o in applied if not any(o is g for g in gone)]
            for g in gone:
                reject(g, 'rolled back: the new file was not indexed')
    return applied, rejected


# ---------- gate ----------

def gate(before, stage, applied, rejected, denials=None):
    """Deterministic checks on a staged memory folder against its snapshot. Returns (code, problems, warnings)."""
    problems, warnings = [], []
    mb, ms = manifest(before), manifest(stage)
    archived = {k for k in ms if k.startswith('archive' + os.sep)}
    for rel, h in ms.items():
        if rel.endswith('.md'):
            continue
        if mb.get(rel) != h:
            problems.append('non-markdown file added or changed: %s' % rel)
    for rel in mb:
        if rel in ms:
            continue
        arch = os.path.join('archive', rel)
        if arch in ms and ms[arch] == mb[rel]:
            continue
        problems.append('file removed without an archive copy: %s' % rel)
    for rel in ms:
        if not rel.endswith('.md') or rel == 'MEMORY.md' or rel in archived or mb.get(rel) == ms[rel]:
            continue
        p = os.path.join(stage, rel)
        if rel in mb and os.path.getsize(p) < os.path.getsize(os.path.join(before, rel)):
            problems.append('topic file shrank: %s' % rel)
        if not read(p).startswith('---'):
            problems.append('topic file lost its frontmatter: %s' % rel)
    for rel in sorted(PROTECTED & set(mb)):
        if rel not in ms:
            problems.append('protected file removed or moved: %s' % rel)
        elif ms[rel] != mb[rel] and not read(os.path.join(stage, rel)).startswith(read(os.path.join(before, rel))):
            problems.append('protected file rewritten, not appended: %s' % rel)
    ipath = index_path(stage)
    if os.path.isfile(ipath):
        old_lines = set(read(index_path(before)).split('\n')) if os.path.isfile(index_path(before)) else set()
        new_text = read(ipath)
        for line in new_text.split('\n'):
            if line.strip() and line not in old_lines and (
                    not INDEX_LINE_RE.match(line) or len(line) > INDEX_LINE_CAP
                    or any(not FILE_RE.match(t) for t in re.findall(r'\]\(([^)]*)\)', line))):
                problems.append('MEMORY.md has a changed or added line that is not a valid index line: %s' % line[:80])
        for f in sorted(PROTECTED & set(mb)):
            for line in old_lines:
                if '](%s)' % f in line and line not in new_text.split('\n'):
                    problems.append('index line of protected file %s was changed or removed' % f)
        old_index = read(index_path(before)).split('\n') if os.path.isfile(index_path(before)) else []
        for key, old, succ in index_line_successors(old_index, new_text.split('\n')):
            if succ is not None:
                dropped = lost_guard_terms(old, succ)
                if dropped:
                    problems.append('index line of %s dropped warning or correction wording: %s' % (key, ', '.join(dropped)))
                continue
            counts, spans = guard_terms(old)
            if counts or spans:
                warnings.append('index line of %s removed%s; it held warning or correction wording: %s' % (
                    key, ' (file archived)' if os.path.join('archive', key) in ms else '',
                    ', '.join(sorted(counts) + ['"%s"' % s for s in dict.fromkeys(spans)])))
        for rel in sorted(ms):
            if rel.endswith('.md') and rel != 'MEMORY.md' and rel not in mb and rel not in archived \
                    and '](%s)' % rel not in new_text:
                problems.append('new topic file has no index line: %s' % rel)
        if os.path.getsize(ipath) > INDEX_BYTES_CAP:
            problems.append('MEMORY.md is %d bytes, cap %d' % (os.path.getsize(ipath), INDEX_BYTES_CAP))
        before_idx = read(index_path(before)) if os.path.isfile(index_path(before)) else ''
        old_broken = {f for f in LINK_RE.findall(before_idx) if not os.path.isfile(os.path.join(before, f))}
        for f in LINK_RE.findall(read(ipath)):
            if not os.path.isfile(os.path.join(stage, f)):
                (warnings if f in old_broken else problems).append('index link does not resolve: %s' % f)
    else:
        problems.append('MEMORY.md missing')
    if len(applied) > MAX_OPS:
        problems.append('%d operations applied, cap %d' % (len(applied), MAX_OPS))
    keys = [json.dumps(o, sort_keys=True, default=str) for o in applied]
    if len(set(keys)) < len(keys):  # apply refuses a repeated operation, so a repeat here is a flood or a forged list
        problems.append('%d identical operations in the applied list' % (len(keys) - len(set(keys))))
    cut = [r for r in rejected if isinstance(r, dict) and str(r.get('reason', '')).startswith('over the ')
           and 'operation cap' in str(r.get('reason', ''))]
    if cut:
        warnings.append('%d operation(s) cut by the %d operation cap' % (len(cut), MAX_OPS))
    for r in rejected:
        op = r.get('op') if isinstance(r, dict) else None
        if isinstance(op, dict) and op.get('file') in PROTECTED:
            warnings.append('a rejected operation targeted protected file %s' % op['file'])
    if denials:
        warnings.append('%d permission denial(s) in model calls, first: %s %s' % (
            len(denials), denials[0].get('tool'), denials[0].get('path')))
    return (2 if problems else 1 if warnings else 0), problems, warnings


# ---------- due ----------

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def is_due(state, sessions, t=None):
    """Due when 24 h passed since the last finished run, 3 sessions with human turns started since, no live lock and
    no run waiting for review. `sessions` must already exclude the current one."""
    t = t or now()
    if state.get('pending_review'):
        return False, 'run %s waits for review' % state['pending_review']
    lock = state.get('lock')
    if lock:
        since = parse_time(lock.get('since'))
        if _pid_alive(lock.get('pid')) and since and (t - since).total_seconds() < LOCK_STALE_HOURS * 3600:
            return False, 'lock held by pid %s' % lock.get('pid')
    last = parse_time(state.get('last_finished'))
    if last and (t - last).total_seconds() < MIN_HOURS * 3600:
        return False, '%.1f h since the last run, need %d' % ((t - last).total_seconds() / 3600, MIN_HOURS)
    resume = cursor_of(state.get('next_since'))
    fresh = [s for s in sessions if s.get('n_user', 0) > 0 and (
        (resume and (s.get('start') or '', s.get('sid') or '') >= resume) or not last or (parse_time(s.get('start')) or t) > last)]
    if len(fresh) < MIN_SESSIONS:
        return False, '%d sessions since the last run, need %d' % (len(fresh), MIN_SESSIONS)
    return True, '%d sessions since the last run' % len(fresh)


# ---------- probes ----------

def check_probes(probes, renders, mem):
    """Each probe's quote must sit in its source: a session render (retention, currency) or a memory file of the
    snapshot (regression)."""
    problems = []
    for p in probes:
        q = squash(p.get('quote'))
        if len(q) < MIN_QUOTE:
            problems.append({'id': p.get('id'), 'why': 'quote too short'})
            continue
        if p.get('family') == 'regression':
            f = p.get('file') or ''
            path = os.path.join(mem, f)
            ok = bool(FILE_RE.match(f)) and os.path.isfile(path) and q in squash(read(path))
        else:
            ok = q in squash(renders.get(p.get('session'), ''))
        if not ok:
            problems.append({'id': p.get('id'), 'why': 'quote not found in its source'})
        if not p.get('question') or not p.get('gold'):
            problems.append({'id': p.get('id'), 'why': 'question or gold missing'})
    return problems


# ---------- headless model calls ----------

def _skip_links(folder, names):
    return [n for n in names if os.path.islink(os.path.join(folder, n))]


def _sandbox_parent():
    """A temporary folder outside the home folder (an ancestor CLAUDE.md or settings would otherwise load)."""
    home = os.path.realpath(HOME)
    for cand in (tempfile.gettempdir(), '/private/tmp', '/tmp'):
        real = os.path.realpath(cand)
        if os.path.isdir(real) and real != home and not real.startswith(home + os.sep):
            return real
    raise OSError('no temporary folder outside the home folder')


def _stage_inputs(box, cwd, inputs):
    """Fill the sandbox: only `inputs` ({relative path: source file or folder}) or else a copy of the cwd folder.
    Symlinks are never followed or copied (their target could sit outside what the call may see)."""
    if inputs is None:
        if cwd:
            shutil.copytree(cwd, box, dirs_exist_ok=True, symlinks=True, ignore=_skip_links)
        return
    for rel, src in inputs.items():
        norm = os.path.normpath(rel)
        if os.path.isabs(norm) or norm.startswith('..'):
            raise ValueError('input path must stay inside the sandbox: %s' % rel)
        if os.path.islink(src):
            raise ValueError('input must not be a symlink: %s' % src)
        dst = os.path.join(box, norm)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True, ignore=_skip_links)
        else:
            shutil.copy2(src, dst)


SANDBOX_TRACE_RE = re.compile(r'^-(?:private-)?(?:var-folders-[A-Za-z0-9_]+-[A-Za-z0-9_]+-T|tmp(?:-[A-Za-z0-9_.-]+)?)'
                              r'-oneiros-[a-z0-9_-]{8}$')  # a temporary folder, then mkdtemp('oneiros-')'s 8 characters


def sandbox_traces(box=None):
    """Folders Claude Code created under ~/.claude/projects for ONEIROS sandboxes: the one of `box` (by its exact
    mangled path), or all that look like one. Symlinks are never returned."""
    root = os.path.join(HOME, '.claude', 'projects')
    if box is None:
        try:
            names = [n for n in os.listdir(root) if SANDBOX_TRACE_RE.match(n)]
        except OSError:
            return []
    else:
        real = os.path.realpath(box)
        if not os.path.basename(real).startswith('oneiros-'):
            return []
        names = {re.sub(r'[^A-Za-z0-9]', '-', real), re.sub(r'[^A-Za-z0-9_]', '-', real)}
    def sandbox_only(p):
        return os.path.isdir(p) and not os.path.islink(p) and not any(f.endswith('.jsonl') for f in os.listdir(p))
    return sorted(os.path.join(root, n) for n in names if sandbox_only(os.path.join(root, n)))


def _denials(d):
    out = []
    for x in d.get('permission_denials') or []:
        i = x.get('tool_input') if isinstance(x, dict) else None
        i = i if isinstance(i, dict) else {}
        out.append({'tool': x.get('tool_name') if isinstance(x, dict) else None,
                    'path': i.get('file_path') or i.get('path') or i.get('pattern')})
    return out


def claude_call(prompt, schema, cwd=None, model='sonnet', effort='medium', budget=2.0, tools=('Read', 'Glob', 'Grep'),
                timeout=1800, inputs=None):
    """One isolated headless call: no CLAUDE.md, no user settings, agents, skills or MCP, no auto-memory, no
    transcript. It runs in a fresh temporary folder that holds only `inputs` (or a copy of `cwd`), with permission
    mode dontAsk so anything outside is denied; the folder is removed afterwards. Returns (structured output or None,
    info); info['denials'] lists the denied tool calls."""
    args = ['claude', '-p', prompt, '--model', model, '--effort', effort, '--setting-sources', 'local',
            '--settings', json.dumps({'autoMemoryEnabled': False}), '--no-session-persistence', '--strict-mcp-config',
            '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
            '--output-format', 'json', '--json-schema', json.dumps(schema), '--max-budget-usd', str(budget)]
    args += ['--tools'] + list(tools) if tools else ['--tools', '']
    try:
        box = tempfile.mkdtemp(prefix='oneiros-', dir=_sandbox_parent())
    except OSError as e:
        return None, {'error': 'no sandbox', 'detail': str(e)[:300], 'denials': []}
    try:
        _stage_inputs(box, cwd, inputs)
        t0 = now()
        try:
            p = subprocess.run(args, cwd=box, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, {'error': 'timeout', 'seconds': timeout, 'denials': []}
        except FileNotFoundError:
            return None, {'error': 'the claude CLI is not on PATH: install Claude Code and sign in (claude auth login)',
                          'denials': []}
        except OSError as e:
            return None, {'error': 'the claude CLI could not start: %s' % e.__class__.__name__, 'denials': []}
    finally:
        shutil.rmtree(box, ignore_errors=True)
        for trace in sandbox_traces(box):  # Claude Code keeps large tool outputs of the call under ~/.claude/projects
            shutil.rmtree(trace, ignore_errors=True)
    info = {'exit': p.returncode, 'seconds': round((now() - t0).total_seconds(), 1), 'denials': []}
    try:
        d = json.loads(p.stdout)
    except ValueError:  # the error class goes to command output; the raw text, which a model may have written, only to calls.json
        info['error'] = 'model call failed (exit %s)' % p.returncode
        info['detail'] = (p.stderr or p.stdout)[-400:]
        return None, info
    u = d.get('usage') or {}
    info.update(cost_usd=d.get('total_cost_usd'), turns=d.get('num_turns'), is_error=d.get('is_error'),
                tokens=sum(int(u.get(k) or 0) for k in ('input_tokens', 'cache_creation_input_tokens',
                                                          'cache_read_input_tokens', 'output_tokens')),
                output_tokens=u.get('output_tokens'), denials=_denials(d))
    out = d.get('structured_output')
    if out is None and d.get('result'):
        try:
            out = json.loads(d['result'])
        except ValueError:
            info['error'] = 'no structured output'
            info['detail'] = str(d.get('result'))[:300]
    return out, info


def protected_rule():
    """The prompt's sentence about append-only files, from the configured PROTECTED list (configured order first)."""
    names = [n for n in PROTECTED_ORDER if n in PROTECTED] + sorted(set(PROTECTED) - set(PROTECTED_ORDER))
    if not names:
        return '- No memory file is limited to `append` beyond these rules.'
    listed = ['`%s`' % n for n in names]
    listed = listed[0] if len(listed) == 1 else ', '.join(listed[:-1]) + ' and ' + listed[-1]
    return '- %s %s only `append`.' % (listed, 'accepts' if len(names) == 1 else 'accept')


def prompt_file(name, **fields):
    fields.setdefault('protected_rule', protected_rule())
    text = read(os.path.join(PROMPTS, name))
    for k, v in fields.items():
        text = text.replace('{{%s}}' % k, str(v))
    return text


CANDIDATE_SCHEMA = {
    'type': 'object', 'required': ['candidates'],
    'properties': {'candidates': {'type': 'array', 'items': {
        'type': 'object', 'required': ['kind', 'statement', 'quote', 'role', 'durable_because', 'target'],
        'properties': {
            'kind': {'type': 'string', 'enum': ['decision', 'preference', 'correction', 'project_state', 'reference',
                                                'lesson', 'outcome']},
            'statement': {'type': 'string'}, 'quote': {'type': 'string'},
            'role': {'type': 'string', 'enum': ['user', 'assistant', 'pasted']}, 'date': {'type': 'string'},
            'durable_because': {'type': 'string'}, 'target': {'type': 'string'}}}}}}

OPS_SCHEMA = {
    'type': 'object', 'required': ['ops', 'flags', 'summary'],
    'properties': {
        'ops': {'type': 'array', 'items': {'type': 'object', 'required': ['op', 'file'], 'properties': {
            'op': {'type': 'string', 'enum': list(OPS)}, 'file': {'type': 'string'}, 'text': {'type': 'string'},
            'old': {'type': 'string'}, 'new': {'type': 'string'}, 'date': {'type': 'string'},
            'line': {'type': 'string'}, 'name': {'type': 'string'}, 'description': {'type': 'string'},
            'type': {'type': 'string'}, 'body': {'type': 'string'}, 'reason': {'type': 'string'},
            'role': {'type': 'string'}, 'sources': {'type': 'array', 'items': {'type': 'string'}},
            'cands': {'type': 'array', 'items': {'type': 'string'}}}}},
        'dispositions': {'type': 'array', 'items': {'type': 'object', 'required': ['cand', 'status'], 'properties': {
            'cand': {'type': 'string'}, 'status': {'type': 'string', 'enum': ['op', 'present', 'skip']},
            'evidence': {'type': 'string'}, 'reason': {'type': 'string', 'enum': list(SKIP_REASONS)}}}},
        'flags': {'type': 'array', 'items': {'type': 'string'}}, 'summary': {'type': 'string'}}}


def _stage_topic_text(stage_dir, target):
    """Squashed text of the target topic file of the stage folder, or, when the target is 'new' or not a topic file
    there, a list of the squashed text of every topic file (MEMORY.md excluded)."""
    def get(name):
        with open(os.path.join(stage_dir, name), encoding='utf-8', errors='replace') as f:
            return squash(f.read())
    if isinstance(target, str) and FILE_RE.match(target) and target.lower() != 'memory.md' \
            and os.path.isfile(os.path.join(stage_dir, target)):
        return [get(target)]
    try:
        names = sorted(n for n in os.listdir(stage_dir) if n.endswith('.md') and n.lower() != 'memory.md'
                       and os.path.isfile(os.path.join(stage_dir, n)))
    except OSError:
        names = []
    return [get(n) for n in names]


def _evidence_ok(cand, evidence, stage_dir):
    """A 'present' disposition holds when the evidence is at least MIN_EVIDENCE characters, occurs verbatim (whitespace
    collapsed) in the candidate's target stage file (any topic file when the target is new), and carries at least one
    number or upper-case name of the candidate's statement when it has any."""
    ev = squash(evidence) if isinstance(evidence, str) else ''
    if len(ev) < MIN_EVIDENCE or not any(ev in t for t in _stage_topic_text(stage_dir, cand.get('target'))):
        return False
    keys = {t.lower() for t in fact_tokens(str(cand.get('statement') or ''))}
    return not keys or bool(keys & ({t.lower() for t in fact_tokens(ev)} | _words(ev)))


def coverage(candidates, ops, dispositions, stage_dir):
    """Final status of every candidate id: 'op' when some op cites the id in `cands`; 'present' when a disposition
    gives valid evidence (see _evidence_ok); 'skip' when a disposition gives an allowed reason; else 'uncovered'. What
    the model claims is data: only these checks decide."""
    cited = set()
    for o in ops if isinstance(ops, list) else []:
        if isinstance(o, dict) and isinstance(o.get('cands'), list):
            cited.update(x for x in o['cands'] if isinstance(x, str))
    disp = {}
    for d in dispositions if isinstance(dispositions, list) else []:
        if isinstance(d, dict) and isinstance(d.get('cand'), str):
            disp.setdefault(d['cand'], []).append(d)
    out = {}
    for c in candidates:
        cid = c.get('id')
        mine = disp.get(cid, [])
        if cid in cited:
            out[cid] = 'op'
        elif any(d.get('status') == 'present' and _evidence_ok(c, d.get('evidence'), stage_dir) for d in mine):
            out[cid] = 'present'
        elif any(d.get('status') == 'skip' and d.get('reason') in SKIP_REASONS for d in mine):
            out[cid] = 'skip'
        else:
            out[cid] = 'uncovered'
    return out


# ---------- run commands ----------

def run_dir(rid):
    if not isinstance(rid, str) or not RUN_ID_RE.fullmatch(rid):
        raise ValueError('bad run id: %r' % (rid,))
    return os.path.join(RUNS, rid)


def next_run_id():
    os.makedirs(RUNS, mode=0o700, exist_ok=True)
    nums = [int(n[1:].split('-')[0]) for n in os.listdir(RUNS) if re.match(r'^R\d+', n)]
    return 'R%d-%s' % (max(nums or [0]) + 1, now().strftime('%Y%m%d-%H%M'))


def current_session_ids():
    """The session this command runs in, so a dream never reads its own half-written transcript. Claude Code sets
    CLAUDE_CODE_SESSION_ID; CLAUDE_SESSION_ID is kept for older builds."""
    return {v for v in (os.environ.get('CLAUDE_CODE_SESSION_ID'), os.environ.get('CLAUDE_SESSION_ID')) if v}


def select_sessions(since=None, until=None, exclude=(), project=PROJECT):
    out = []
    for s in session_index(project):
        if s['n_user'] <= 0 or not s['start'] or s['sid'] in exclude:
            continue
        day = s['start'][:10]
        if (since and day < since) or (until and day > until):
            continue
        out.append(s)
    return out


def chain_base(rid, eval_run):
    """A run another run may chain after: it exists, has a staged folder and a gate that did not withhold it, and is of
    the same kind (live or evaluation). Returns (run, None) or (None, why)."""
    d = run_dir(rid)
    prev = load(os.path.join(d, 'run.json'), None)
    if not isinstance(prev, dict) or not os.path.isdir(os.path.join(d, 'stage')):
        return None, 'no run %s to chain after' % rid
    g = load(os.path.join(d, 'gate.json'), None)
    if not isinstance(g, dict) or g.get('code') not in (0, 1):
        return None, 'run %s has no passing gate (code %s); chain only after a gated run' % (rid, (g or {}).get('code'))
    if bool(prev.get('eval')) != eval_run:
        return None, 'run %s is %s; a chain may not mix live and evaluation runs' % (rid, 'an evaluation run' if prev.get('eval') else 'a live run')
    return prev, None


def reason_class(text):
    """A rejection or gate reason without the values a model or a transcript supplied: quoted parts and everything
    after the first colon dropped, cleaned and capped. The full reason stays in the run's files."""
    s = re.sub(r"'[^']*'|\"[^\"]*\"|%r", '', str(text or ''))
    return clean(s.split(':', 1)[0], 80)


def error_class(text):
    """A model call's error for command output: one of the fixed classes claude_call sets, else a generic one, so text a
    model or a tool wrote never reaches the output."""
    s = str(text or '')
    if s in ('timeout', 'no sandbox', 'no structured output') or re.fullmatch(r'model call failed \(exit -?\d+\)', s):
        return s
    if s.startswith('the claude CLI is not on PATH'):
        return 'the claude CLI is not on PATH: install Claude Code and sign in (claude auth login)'
    if s.startswith('the claude CLI could not start'):
        return 'the claude CLI could not start'
    return 'model call failed'


def safe_info(info):
    """A model call's info for command output: numbers and a reason class only (the error text stays in calls.json)."""
    info = info if isinstance(info, dict) else {}
    out = {k: info.get(k) for k in ('exit', 'seconds', 'cost_usd', 'turns', 'tokens') if k in info}
    if info.get('error'):
        out['error'] = error_class(info['error'])
    out['denials'] = len(info.get('denials') or [])
    return out


def need_folder(path, what):
    """None when `path` is an existing folder, else an error message that says how to configure it."""
    if path and os.path.isdir(path):
        return None
    return ('%s folder %s; set it in oneiros.config.json or the environment (ONEIROS_PROJECT, ONEIROS_MEMORY), '
            'see README.md' % (what, 'not found: %s' % path if path else 'not configured'))


def stopped_runs(state):
    """Live runs started after the last finished dream that never reached their report and were not dismissed."""
    last = state.get('last_finished') or ''
    out = []
    for n in sorted(os.listdir(RUNS)) if os.path.isdir(RUNS) else []:
        path = os.path.join(RUNS, n, 'run.json')
        if not os.path.isfile(path) or os.path.exists(os.path.join(RUNS, n, 'REPORT.md')):
            continue
        r = load(path, None)
        if not isinstance(r, dict):
            out.append(n)  # a run record that cannot be read is a stopped run too
        elif not r.get('eval') and (r.get('started') or '') > last and r.get('status') != 'dismissed':
            out.append(n)
    return out


def _skip_links_and(patterns):
    base = shutil.ignore_patterns(*patterns)
    def ignore(folder, names):
        return set(base(folder, names)) | set(_skip_links(folder, names))
    return ignore


def symlinks_in(folder):
    return sorted(os.path.relpath(os.path.join(b, n), folder) for b, ds, fs in os.walk(folder) for n in ds + fs
                  if os.path.islink(os.path.join(b, n)))


def cursor_of(value):
    """The (start, sid) a previous dream left off at, or None."""
    if isinstance(value, list) and len(value) == 2 and all(isinstance(x, str) for x in value):
        return tuple(value)
    return None


def cmd_start(a):
    state = load(STATE, {})
    raw = state.get('last_finished')
    last = raw[:10] if isinstance(raw, str) and re.match(r'^\d{4}-\d{2}-\d{2}', raw) else None
    cursor = cursor_of(state.get('next_since')) if not a.eval and not a.since else None
    if cursor:
        last = cursor[0][:10]  # the previous dream left sessions for this one (MAX_SESSIONS)
    if not a.eval:
        stopped = stopped_runs(state)
        if len(stopped) >= MAX_STOPPED_RUNS:
            return emit({'error': '%d dreams in a row stopped before their report (%s); look at them and dismiss them '
                                  'before a new one starts' % (len(stopped), ', '.join(stopped))}, 2)
    if not a.eval and not a.since and not last:
        return emit({'error': 'no finished dream in state.json and no --since: pass --since YYYY-MM-DD so a first run '
                              'does not read the whole session history'}, 2)
    after = getattr(a, 'after', None)
    if after:
        prev, why = chain_base(after, bool(a.eval))
        if why:
            return emit({'error': why}, 2)
        if a.memory:
            return emit({'error': '--after and --memory exclude each other'}, 2)
        src, target = os.path.join(run_dir(after), 'stage'), prev.get('memory_source') or LIVE_MEMORY
    else:
        src = target = a.memory or LIVE_MEMORY
    why = need_folder(src, 'memory') or need_folder(a.project or PROJECT, 'Claude Code project')
    if why:
        return emit({'error': why}, 2)
    if os.path.islink(index_path(src)):
        return emit({'error': 'MEMORY.md in %s is a symlink; ONEIROS never reads or writes through links' % src}, 2)
    if not os.path.isfile(index_path(src)):
        return emit({'error': 'no MEMORY.md in %s; create one (it may be empty) before the first dream' % src}, 2)
    if not a.eval and swap_running():
        return emit({'error': 'a swap or undo is running; start the dream when it has finished'}, 2)
    rid = a.id or next_run_id()
    d = run_dir(rid)
    if os.path.exists(d):
        return emit({'error': 'run folder exists: %s' % d}, 2)
    exclude = set(a.exclude or []) | current_session_ids()
    sessions = select_sessions(a.since or last, a.until, exclude, a.project or PROJECT)
    if cursor:
        sessions = [s for s in sessions if (s['start'] or '', s['sid']) >= cursor]
    if not sessions:
        return emit({'error': 'no sessions with human turns in the window'}, 2)
    deferred = []
    if not a.eval and len(sessions) > MAX_SESSIONS:
        if a.until:
            return emit({'error': 'the window holds %d sessions, more than %d per dream; split it into batches '
                                  '(chain_dream.sh)' % (len(sessions), MAX_SESSIONS)}, 2)
        sessions, deferred = sessions[:MAX_SESSIONS], sessions[MAX_SESSIONS:]
    os.makedirs(RUNS, mode=0o700, exist_ok=True)
    os.makedirs(d, mode=0o700)
    os.chmod(d, 0o700)
    save(os.path.join(d, 'run.json'), {'id': rid, 'started': iso(now()), 'status': 'starting', 'eval': bool(a.eval)})
    os.makedirs(os.path.join(d, 'renders'))
    links = symlinks_in(src)
    shutil.copytree(src, os.path.join(d, 'before'), symlinks=True,
                    ignore=_skip_links_and(('.consolidate-lock', '.git', '.oneiros-*.tmp')))
    shutil.copytree(os.path.join(d, 'before'), os.path.join(d, 'stage'), symlinks=True)
    rendered = []
    for s in sessions:
        text, meta = render_session(s['path'])
        write(os.path.join(d, 'renders', s['sid'] + '.txt'), text)
        rendered.append(meta)
    run = {'id': rid, 'started': iso(now()), 'memory_source': target, 'snapshot_source': src, 'after': after,
           'window': [a.since or last, a.until],
           'sessions': [m['sid'] for m in rendered], 'renders': rendered, 'before_manifest': manifest(os.path.join(d, 'before')),
           'status': 'started', 'eval': bool(a.eval), 'skipped_links': links,
           'deferred': [s['sid'] for s in deferred], 'used_cursor': list(cursor) if cursor else None,
           'next_since': [deferred[0]['start'] or '', deferred[0]['sid']] if deferred else None}
    save(os.path.join(d, 'run.json'), run)
    if not a.eval:
        with state_update() as state:  # under the lock: a swap or dismiss may write it while this run copied and rendered
            state['lock'] = {'pid': os.getpid(), 'since': iso(now()), 'run': rid}
    return emit({'run': rid, 'dir': d, 'sessions': len(rendered), 'render_chars': sum(m['chars'] for m in rendered),
                 'deferred': len(deferred), 'skipped_links': links, 'protected': sorted(PROTECTED)})


def _calls_log(d, step, info):
    log = load(os.path.join(d, 'calls.json'), [])
    log.append(dict(info, step=step, t=iso(now())))
    save(os.path.join(d, 'calls.json'), log)


def cmd_analyze(a):
    d = run_dir(a.run)
    run = load(os.path.join(d, 'run.json'), {})
    os.makedirs(os.path.join(d, 'candidates'), exist_ok=True)
    work = [s for s in run['sessions'] if not os.path.exists(os.path.join(d, 'candidates', s + '.json'))]

    def one(sid):
        prompt = prompt_file('analyze.md', render='renders/%s.txt' % sid, index='before/MEMORY.md', sid=sid)
        out, info = claude_call(prompt, CANDIDATE_SCHEMA, model=a.model, effort=a.effort, budget=a.budget,
                                inputs={'renders/%s.txt' % sid: os.path.join(d, 'renders', sid + '.txt'),
                                        'before/MEMORY.md': index_path(os.path.join(d, 'before'))})
        return sid, out, info

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for sid, out, info in ex.map(one, work):
            _calls_log(d, 'analyze:' + sid[:8], info)
            if out is None:
                results.append({'sid': sid, 'error': error_class(info.get('error'))})
                continue
            render = read(os.path.join(d, 'renders', sid + '.txt'))
            kept, dropped = verify_candidates(render, out.get('candidates') or [], sid)
            save(os.path.join(d, 'candidates', sid + '.json'), {'kept': kept, 'dropped': dropped})
            results.append({'sid': sid, 'kept': len(kept), 'dropped': len(dropped)})
    failed = [r for r in results if 'error' in r]
    return emit({'run': a.run, 'analyzed': len(results) - len(failed), 'failed': failed,
                 'kept': sum(r.get('kept', 0) for r in results), 'dropped': sum(r.get('dropped', 0) for r in results)},
                2 if failed else 0)


def _run_candidates(d, run):
    """Kept candidates of a run with their ids, or None (name of the session that lacks a file) when analyze is missing."""
    cands = []
    for sid in run['sessions']:
        c = load(os.path.join(d, 'candidates', sid + '.json'), None)
        if c is None:
            return None, sid
        cands += [dict(x, id=x.get('id') or 'c%s-%d' % (sid[:8], n)) for n, x in enumerate(c['kept'], 1)]
    return cands, None


RETRY_NOTE = (
    '\n\nThis is the retry call. A first call already handled the other candidates. `uncovered.json` lists the '
    'candidates that got no valid disposition: give each of them an operation that cites its id in `cands`, or a '
    '`present` disposition whose `evidence` is a verbatim snippet (at least %d characters) of its target file that '
    'holds its key number or name, or a `skip` disposition with one allowed reason. Return ops and dispositions for '
    'those candidates only, and at most {{max_ops}} operations.' % MIN_EVIDENCE)


def well_formed(out):
    """The consolidate answer with ops and dispositions as lists of objects and flags as a list of strings."""
    out = out if isinstance(out, dict) else {}
    def listed(v, kind):
        return [x for x in v if isinstance(x, kind)] if isinstance(v, list) else []
    return dict(out, ops=listed(out.get('ops'), dict), dispositions=listed(out.get('dispositions'), dict),
                flags=listed(out.get('flags'), str), summary=out.get('summary') if isinstance(out.get('summary'), str) else '')


def cmd_consolidate(a):
    d = run_dir(a.run)
    run = load(os.path.join(d, 'run.json'), {})
    cands, missing = _run_candidates(d, run)
    if cands is None:
        return emit({'error': 'candidates missing for %s; run analyze first' % missing}, 2)
    save(os.path.join(d, 'candidates.json'), cands)
    sessions = ', '.join(s[:8] for s in run['sessions'])
    today = a.today or now().date().isoformat()
    inputs = {'stage': os.path.join(d, 'stage'), 'candidates.json': os.path.join(d, 'candidates.json')}
    prompt = prompt_file('consolidate.md', today=today, sessions=sessions, n=len(cands), max_ops=MAX_OPS)
    out, info = claude_call(prompt, OPS_SCHEMA, model=a.model, effort=a.effort, budget=a.budget,
                            tools=('Read', 'Glob', 'Grep'), inputs=inputs)
    _calls_log(d, 'consolidate', info)
    if out is None:
        return emit({'error': 'consolidate failed', 'info': safe_info(info)}, 2)
    out = well_formed(out)
    stage = os.path.join(d, 'stage')
    cov = coverage(cands, out.get('ops') or [], out.get('dispositions') or [], stage)
    uncovered = [c for c in cands if cov[c['id']] == 'uncovered']
    retried, cost, tokens = False, info.get('cost_usd'), info.get('tokens')
    if uncovered:
        retried = True
        save(os.path.join(d, 'uncovered.json'), uncovered)
        left = max(0, MAX_OPS - len(out.get('ops') or []))
        retry_prompt = prompt_file('consolidate.md', today=today, sessions=sessions, n=len(cands), max_ops=left) \
            + RETRY_NOTE.replace('{{max_ops}}', str(left))
        out2, info2 = claude_call(retry_prompt, OPS_SCHEMA, model=a.model, effort=a.effort, budget=a.budget,
                                  tools=('Read', 'Glob', 'Grep'),
                                  inputs=dict(inputs, **{'uncovered.json': os.path.join(d, 'uncovered.json')}))
        _calls_log(d, 'consolidate-retry', info2)
        cost = (cost or 0) + (info2.get('cost_usd') or 0)
        tokens = (tokens or 0) + (info2.get('tokens') or 0)
        if out2 is None:
            out = dict(out, flags=list(out.get('flags') or []) + ['coverage retry failed: %s' % info2.get('error')])
        else:
            first = list(out.get('ops') or [])
            out2 = well_formed(out2)
            extra = [o for o in (out2.get('ops') or []) if o not in first]
            out = dict(out, ops=first + extra[:left],
                       dispositions=list(out.get('dispositions') or []) + list(out2.get('dispositions') or []),
                       flags=list(out.get('flags') or []) + list(out2.get('flags') or [])
                       + (['retry ops cut to the %d left under the cap' % left] if len(extra) > left else []))
        cov = coverage(cands, out.get('ops') or [], out.get('dispositions') or [], stage)
    counts = {k: sum(1 for v in cov.values() if v == k) for k in ('op', 'present', 'skip', 'uncovered')}
    save(os.path.join(d, 'coverage.json'), {
        'status': cov, 'counts': counts, 'retried': retried,
        'uncovered': [{'id': c['id'], 'statement': c.get('statement')} for c in cands if cov[c['id']] == 'uncovered']})
    save(os.path.join(d, 'ops.json'), out)
    return emit({'run': a.run, 'ops': len(out.get('ops') or []), 'flags': len(out.get('flags') or []),
                 'coverage': counts, 'retried': retried, 'cost_usd': cost, 'tokens': tokens})


def cmd_mechanical(a):
    """Control arm C: no model call. Copies the dream run's snapshot, renders and candidates into a new run and writes
    one append op per kept candidate whose target is an existing topic file that lacks some of its numbers or names.
    `apply` and `gate` then treat it like any run."""
    src, d = run_dir(a.from_run), run_dir(a.run)
    run = load(os.path.join(src, 'run.json'), None)
    if run is None:
        return emit({'error': 'no such dream run: %s' % a.from_run}, 2)
    if os.path.exists(d):
        return emit({'error': 'run folder exists: %s' % d}, 2)
    cands, missing = _run_candidates(src, run)
    if cands is None:
        return emit({'error': 'candidates missing for %s in %s' % (missing, a.from_run)}, 2)
    shutil.copytree(os.path.join(src, 'before'), os.path.join(d, 'before'))
    shutil.copytree(os.path.join(d, 'before'), os.path.join(d, 'stage'))
    shutil.copytree(os.path.join(src, 'renders'), os.path.join(d, 'renders'))
    shutil.copytree(os.path.join(src, 'candidates'), os.path.join(d, 'candidates'))
    save(os.path.join(d, 'candidates.json'), cands)
    ops = mechanical_ops(cands, os.path.join(d, 'stage'))
    today = a.today or now().date().isoformat()
    save(os.path.join(d, 'run.json'), dict(
        run, id=a.run, started=iso(now()), status='started', eval=True, mechanical=True, today=today,
        **{'from': a.from_run, 'before_manifest': manifest(os.path.join(d, 'before'))}))
    save(os.path.join(d, 'ops.json'), {
        'source': 'mechanical', 'from': a.from_run, 'ops': ops, 'flags': [],
        'summary': 'Mechanical control from %s: %d append ops, no model call.' % (a.from_run, len(ops))})
    return emit({'run': a.run, 'dir': d, 'from': a.from_run, 'candidates': len(cands), 'ops': len(ops)})


def _words(text):
    return set(re.findall(r'[a-z0-9]+', (text or '').lower()))


def mechanical_ops(candidates, stage_dir):
    """One append op per candidate whose target is an existing topic file (not MEMORY.md, not 'new') and whose numbers
    and names (fact_tokens plus capitalised words of two or more letters) are not all in that file already."""
    ops, added = [], {}
    for c in candidates:
        target = c.get('target')
        if not (isinstance(target, str) and FILE_RE.match(target) and target.lower() != 'memory.md'
                and os.path.isfile(os.path.join(stage_dir, target))):
            continue
        statement = squash(c.get('statement'))
        # numbers and upper-case names; capitalised words (a sentence start, a nationality) count only when nothing else does
        keys = {t.lower() for t in fact_tokens(statement)} \
            or {w.lower() for w in re.findall(r'\b[A-Z][A-Za-z]+\b', statement)}
        text = read(os.path.join(stage_dir, target)) + '\n' + added.get(target, '')  # earlier ops of this list count too
        have = {t.lower() for t in fact_tokens(text)} | _words(text)
        if not statement or keys <= have:
            continue
        added[target] = added.get(target, '') + '\n' + statement
        ops.append({'op': 'append', 'file': target, 'text': statement, 'sources': [str(c.get('session') or '')[:8]],
                    'cands': [c['id']], 'role': c.get('role') or ''})
    return ops


def cmd_apply(a):
    d = run_dir(a.run)
    if not os.path.isfile(os.path.join(d, 'ops.json')):
        return emit({'error': 'run %s has no ops.json; run consolidate first' % a.run}, 2)
    run = load(os.path.join(d, 'run.json'), {})
    ops = well_formed(load(os.path.join(d, 'ops.json'), {})).get('ops')
    today = a.today or run.get('today') or now().date().isoformat()
    cands = load(os.path.join(d, 'candidates.json'), [])
    over = [{'op': o, 'reason': 'over the %d operation cap' % MAX_OPS} for o in ops[MAX_OPS:]]
    applied, rejected = apply_ops(os.path.join(d, 'stage'), ops[:MAX_OPS], set(run['sessions']), today, cands)
    save(os.path.join(d, 'applied.json'), {'applied': applied, 'rejected': rejected + over})
    cov = load(os.path.join(d, 'coverage.json'), None)
    if isinstance(cov, dict) and isinstance(cov.get('status'), dict):
        # a candidate is only covered by an op that was applied; the others are listed, not counted as written
        done = {x for o in applied for x in (o.get('cands') or []) if isinstance(x, str)}
        stmts = {c.get('id'): c.get('statement') for c in cands}
        cov['not_applied'] = [{'id': k, 'statement': stmts.get(k)} for k, v in cov['status'].items()
                              if v == 'op' and k not in done]
        save(os.path.join(d, 'coverage.json'), cov)
    return emit({'run': a.run, 'applied': len(applied), 'rejected': len(rejected) + len(over)})


def _all_denials(d):
    return [dict(x, step=c.get('step')) for c in load(os.path.join(d, 'calls.json'), []) for x in c.get('denials') or []]


def cmd_gate(a):
    d = run_dir(a.run)
    if not os.path.isfile(os.path.join(d, 'applied.json')):
        return emit({'error': 'run %s has no applied.json; run apply first' % a.run}, 2)
    res = load(os.path.join(d, 'applied.json'), {'applied': [], 'rejected': []})
    denials = _all_denials(d)
    code, problems, warnings = gate(os.path.join(d, 'before'), os.path.join(d, 'stage'), res['applied'], res['rejected'],
                                    denials)
    save(os.path.join(d, 'gate.json'), {'code': code, 'problems': problems, 'warnings': warnings})
    return emit({'run': a.run, 'code': code, 'problems': len(problems), 'warnings': len(warnings)}, code)


def _diffstat(before, stage):
    mb, ms = manifest(before), manifest(stage)
    return {'added': sorted(k for k in ms if k not in mb), 'removed': sorted(k for k in mb if k not in ms),
            'changed': sorted(k for k in ms if k in mb and mb[k] != ms[k]),
            'index_bytes': [os.path.getsize(index_path(x)) if os.path.isfile(index_path(x)) else 0 for x in (before, stage)],
            'longest_index_line': [max(map(len, read(index_path(x)).splitlines() or [''])) if os.path.isfile(index_path(x)) else 0
                                   for x in (before, stage)]}


MAX_FLAGS = 10


def op_text(o):
    """Everything an operation would write, for the person who reviews it (nothing hidden or cut short)."""
    if o.get('op') == 'add_file':
        return 'new file "%s" (%s, type %s): %s' % (o.get('name'), o.get('description'), o.get('type'), o.get('body'))
    if o.get('op') == 'supersede':
        return 'replace "%s" with "%s"' % (o.get('old'), o.get('new'))
    return o.get('text') or o.get('line') or o.get('reason') or o.get('description') or ''


def report_brief(rid, run, res, g, cov, denials, calls, path, nflags):
    """The morning message, built only from counts and codes: no text a model or a transcript wrote."""
    n = (cov or {}).get('counts') or {}
    return ('ONEIROS dream %s: %d sessions read, %d changes staged, %d rejected, gate %s (%d problems, %d warnings), '
            '%d flags, coverage op %d present %d skip %d uncovered %d, %d permission denials, USD %.2f%s. Report: %s'
            % (rid, len(run.get('sessions') or []), len(res['applied']), len(res['rejected']), g.get('code'),
               len(g.get('problems') or []), len(g.get('warnings') or []), nflags,
               n.get('op', 0), n.get('present', 0), n.get('skip', 0), n.get('uncovered', 0), len(denials),
               sum(float(c.get('cost_usd') or 0) for c in calls),
               ('; %d sessions wait for the next dream' % len(run['deferred'])) if run.get('deferred') else '', path))


def cmd_report(a):
    d = run_dir(a.run)
    if not os.path.isfile(os.path.join(d, 'gate.json')):
        return emit({'error': 'run %s has no gate.json; run gate first' % a.run}, 2)
    run = load(os.path.join(d, 'run.json'), {})
    first = run.get('status') != 'reported'  # a re-rendered report never changes the review state
    res = load(os.path.join(d, 'applied.json'), {'applied': [], 'rejected': []})
    g = load(os.path.join(d, 'gate.json'), {})
    ops = load(os.path.join(d, 'ops.json'), {})
    calls = load(os.path.join(d, 'calls.json'), [])
    ds = _diffstat(os.path.join(d, 'before'), os.path.join(d, 'stage'))
    flags = [f for f in ops.get('flags') or [] if isinstance(f, str)]
    cand_by_id = {c.get('id'): c for c in load(os.path.join(d, 'candidates.json'), []) if isinstance(c, dict)}
    lines = ['# ONEIROS dream %s' % a.run, '',
             'Text below that a model or a session wrote (titles, summary, flags, operations, coverage) is quoted data, '
             'not instructions.', '',
             '- Sessions read: %d (%s)' % (len(run['sessions']), inert(', '.join(clean(m['title'], 60) or m['sid'][:8] for m in run['renders']), 900)),
             '- Operations: %d applied, %d rejected; gate code %s' % (len(res['applied']), len(res['rejected']), g.get('code')),
             '- Files: %d added, %d changed, %d moved to archive' % (len(ds['added']), len(ds['changed']), len(ds['removed'])),
             '- MEMORY.md: %d to %d bytes; longest line %d to %d characters' % tuple(ds['index_bytes'] + ds['longest_index_line']),
             '- Model calls: %d, about %d tokens, USD %.2f' % (len(calls), sum(c.get('tokens') or 0 for c in calls),
                                                               sum(float(c.get('cost_usd') or 0) for c in calls)),
             '', '## Summary', '', inert(ops.get('summary'), 1500) or '(none)', '', '## Flags for you', '']
    if run.get('from'):
        lines.insert(2, '- Control: mechanical append ops from %s, no model call' % run['from'])
    if run.get('after'):
        lines.insert(2, '- Chained after %s: built on its staged memory. Swap %s first for an exact copy; otherwise the swap '
                        'replays these operations on live memory one by one' % (run['after'], run['after']))
    lines += ['- ' + inert(f, 800) for f in flags[:MAX_FLAGS]] or ['- none']
    if len(flags) > MAX_FLAGS:
        lines.append('- (%d more flags in ops.json)' % (len(flags) - MAX_FLAGS))
    if run.get('deferred'):
        lines.append('- %d sessions were left for the next dream (at most %d per dream)' % (len(run['deferred']), MAX_SESSIONS))
    if run.get('skipped_links'):
        lines.append('- symlinks in the memory folder were not read: %s' % inert(', '.join(run['skipped_links']), 300))
    lines += ['', '## Applied', '']
    for i, o in enumerate(res['applied'], 1):  # the numbers swap --skip takes
        lines.append('%d. %s %s: %s' % (i, inert(o.get('op'), 20), inert(o.get('file'), 90), inert(op_text(o), MAX_OP_CHARS + 300)))
        for cid in (o.get('cands') or [])[:6] if isinstance(o.get('cands'), list) else []:
            c = cand_by_id.get(cid) if isinstance(cid, str) else None
            if c:
                lines.append('   evidence %s (%s): "%s"' % (inert(cid, 30), inert(c.get('role'), 12), inert(c.get('quote'), 300)))
    lines += ['', '## Rejected', '']
    for r in res['rejected']:
        o = r['op'] if isinstance(r['op'], dict) else {}
        lines.append('- %s %s: %s' % (inert(o.get('op'), 20), inert(o.get('file'), 80), inert(r['reason'], 300)))
    cov = load(os.path.join(d, 'coverage.json'), None)
    if cov:
        n = cov.get('counts') or {}
        lines += ['', '## Coverage', '', '- ' + ', '.join('%s %d' % (k, n.get(k, 0)) for k in ('op', 'present', 'skip', 'uncovered'))
                  + ('; retry call made' if cov.get('retried') else '')]
        lines += ['- uncovered %s: %s' % (inert(u.get('id'), 30), inert(u.get('statement'), 300)) for u in cov.get('uncovered') or []]
        lines += ['- cited by an op that was not applied %s: %s' % (inert(u.get('id'), 30), inert(u.get('statement'), 300))
                  for u in cov.get('not_applied') or []]
    denials = _all_denials(d)
    lines += ['', '## Permission denials', ''] + ['- %s: %s %s' % (inert(x.get('step'), 30), inert(x.get('tool'), 40), inert(x.get('path'), 200))
                                                  for x in denials[:20]] \
        + (['- (%d more in calls.json)' % (len(denials) - 20)] if len(denials) > 20 else []) + ([] if denials else ['- none'])
    lines += ['', '## Gate', ''] + ['- problem: ' + inert(p, 300) for p in g.get('problems') or []] \
        + ['- warning: ' + inert(w, 300) for w in g.get('warnings') or []]
    write(os.path.join(d, 'REPORT.md'), '\n'.join(lines) + '\n')
    brief = report_brief(a.run, run, res, g, cov, denials, calls, os.path.join(d, 'REPORT.md'), len(flags))
    if not run.get('eval') and first:
        with state_update() as state:
            state['pending_review'] = a.run
            state.pop('lock', None)
            state['last_finished'] = iso(now())
            if run.get('next_since'):
                state['next_since'] = run['next_since']
            elif run.get('used_cursor') or (cursor_of(state.get('next_since')) and (run.get('window') or [None])[0]
                                            and run['window'][0] <= state['next_since'][0][:10] and not run['window'][1]):
                state.pop('next_since', None)  # this run read the waiting sessions; an unrelated run leaves the cursor
        run['status'] = 'reported'
        save(os.path.join(d, 'run.json'), run)
    if not run.get('eval'):
        if a.notify and first:
            notify('Dream %s: %d changes staged, gate %s. Review REPORT.md' % (a.run, len(res['applied']), g.get('code')))
    return emit({'run': a.run, 'report': os.path.join(d, 'REPORT.md'), 'brief': brief,
                 'diff': {'added': len(ds['added']), 'changed': len(ds['changed']), 'removed': len(ds['removed']),
                          'index_bytes': ds['index_bytes']}})


def notify(message):
    """A desktop notification on macOS (the text goes in as an argument, never into the script); nothing elsewhere."""
    try:
        subprocess.run(['osascript', '-e', 'on run argv', '-e', 'display notification (item 1 of argv) with title "ONEIROS"',
                        '-e', 'end run', clean(message, 200)], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


SWAP_LOCK = '.swap.lock'


def swap_lock_path():
    return os.path.join(RUNS, SWAP_LOCK)


def swap_running():
    """True when another process holds the swap lock (probed without creating the lock file)."""
    try:
        fd = os.open(swap_lock_path(), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def locked(fn):
    """Run a swap or undo under an exclusive flock on runs/.swap.lock; refuse when another process holds it."""
    def run(a):
        with flocked(swap_lock_path(), wait=False) as ok:
            if not ok:
                return emit({'error': 'another swap or undo is running (lock %s); try again when it has finished'
                                      % swap_lock_path()}, 2)
            return fn(a)
    run.__name__ = fn.__name__
    return run


@locked
def cmd_swap(a):
    d = run_dir(a.run)
    run = load(os.path.join(d, 'run.json'), {})
    g = load(os.path.join(d, 'gate.json'), {})
    if run.get('eval'):
        return emit({'error': 'run %s is an evaluation run; it is never swapped into memory' % a.run}, 2)
    if g.get('code') not in (0, 1):
        return emit({'error': 'gate withheld this run' if g.get('code') == 2 else 'run has no gate result; it cannot be swapped'}, 2)
    live = run.get('memory_source') or LIVE_MEMORY
    all_ops = load(os.path.join(d, 'applied.json'), {}).get('applied', [])
    skip = set()
    if getattr(a, 'skip', None) is not None:
        parts = [x.strip() for x in a.skip.split(',') if x.strip()]
        if not parts or not all(re.fullmatch(r'[0-9]+', x) for x in parts):
            return emit({'error': '--skip takes operation numbers as numbered in REPORT.md, for example 3,7'}, 2)
        skip = {int(x) for x in parts}
        bad = sorted(n for n in skip if n < 1 or n > len(all_ops))
        if bad:
            return emit({'error': 'no operation %s in this run (it has %d)' % (', '.join(map(str, bad)), len(all_ops))}, 2)
        if len(skip) == len(all_ops):
            return emit({'error': 'every operation skipped; dismiss the run instead'}, 2)
    backup = os.path.join(d, 'live-backup')
    if os.path.exists(backup):
        if load(os.path.join(d, 'swap.json'), {}).get('status') == 'in-progress':
            return emit({'error': 'a swap of this run was interrupted; run undo to restore live memory from its backup'}, 2)
        return emit({'error': 'already swapped (live-backup exists); undo first'}, 2)
    links = symlinks_in(live)
    if links:
        return emit({'error': 'the memory folder holds symlinks (%s); ONEIROS never writes through links' % ', '.join(links[:5])}, 2)
    tmp = backup + '.tmp'
    shutil.rmtree(tmp, ignore_errors=True)  # a half copy from a crashed swap is never taken for a backup
    shutil.copytree(live, tmp, symlinks=True)
    os.rename(tmp, backup)
    save(os.path.join(d, 'swap.json'), {'status': 'in-progress', 't': iso(now())})
    current = {k: v for k, v in manifest(live).items() if k != '.consolidate-lock' and not k.startswith('.git' + os.sep)}
    if current == run['before_manifest'] and not skip:
        mode = 'copy'
        for rel in manifest(os.path.join(d, 'stage')):
            dst = os.path.join(live, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            copy_atomic(os.path.join(d, 'stage', rel), dst)
        for rel in run['before_manifest']:
            if not os.path.exists(os.path.join(d, 'stage', rel)) and os.path.exists(os.path.join(live, rel)):
                os.remove(os.path.join(live, rel))  # only files the stage archived; their copy sits in archive/
        applied, rejected = load(os.path.join(d, 'applied.json'), {}).get('applied', []), []
    else:
        mode = 'replay'
        ops = [o for i, o in enumerate(all_ops, 1) if i not in skip]  # operations the person chose to leave out are not replayed
        applied, rejected = apply_ops(live, ops, set(run['sessions']), (run.get('started') or iso(now()))[:10],
                                      load(os.path.join(d, 'candidates.json'), []))
        if ops and not applied:  # nothing changed in live, so nothing is recorded as swapped
            shutil.rmtree(backup)
            save(os.path.join(d, 'swap.json'), {'status': 'refused', 'mode': mode, 't': iso(now()), 'applied': [], 'rejected': rejected})
            return emit({'error': 'replay applied 0 of %d operations; nothing swapped (swap the run it was chained after '
                                  'first, or dismiss this one)' % len(ops),
                         'reasons': sorted({reason_class(r['reason']) for r in rejected})}, 2)
    code, problems, warnings = gate(backup, live, applied, rejected)
    stamp = now().strftime('%Y%m%d%H%M%S')
    if code == 2:
        try:  # what the failed swap left, for the record; never a reason to skip the restore
            shutil.copytree(live, fresh(os.path.join(d, 'live-failed-swap-' + stamp)), symlinks=True)
        except Exception as e:
            problems = problems + ['snapshot of the failed swap not kept: %s' % e]
        restore(live, backup)
        failed = fresh(backup + '-failed-' + stamp)
        shutil.move(backup, failed)  # kept for the record; the run is not swapped, so it may be retried
        save(os.path.join(d, 'swap.json'), {'status': 'restored', 'mode': mode, 't': iso(now()), 'problems': problems,
                                             'applied': applied, 'rejected': rejected})
        return emit({'error': 'post-swap gate failed; live memory restored', 'problems': sorted({reason_class(p) for p in problems}),
                     'backup': failed}, 2)
    note = ('%d of the reviewed operations were not applied; see the record' % len(rejected)) if rejected else ''
    skipped = [{'n': i, 'op': o} for i, o in enumerate(all_ops, 1) if i in skip]
    save(os.path.join(d, 'swap.json'), {'status': 'swapped-partial' if rejected else 'swapped', 'mode': mode, 't': iso(now()),
                                         'applied': applied, 'rejected': rejected, 'skipped': skipped, 'warnings': warnings,
                                         'note': note, 'after_manifest': manifest(live)})
    with state_update() as state:
        if state.get('pending_review') == a.run:
            state.pop('pending_review')
        state.setdefault('swaps', []).append({'run': a.run, 'mode': mode, 't': iso(now())})
    return emit({'run': a.run, 'mode': mode, 'applied': len(applied), 'rejected': len(rejected), 'backup': backup,
                 'rejected_reasons': sorted({reason_class(r['reason']) for r in rejected}), 'record': os.path.join(d, 'swap.json'),
                 'skipped': sorted(skip), 'note': note})


def fresh(path):
    """`path`, or `path-2`, `path-3` ... when it exists (two attempts in one second)."""
    out, n = path, 1
    while os.path.exists(out):
        n += 1
        out = '%s-%d' % (path, n)
    return out


def copy_atomic(src, dst):
    """Copy a file through a temporary file and os.replace; a symlink at `dst` is refused."""
    if os.path.islink(dst):
        raise OSError('refusing to write through a symlink: %s' % dst)
    fd, tmp = tempfile.mkstemp(prefix='.oneiros-', suffix='.tmp', dir=os.path.dirname(dst) or '.')
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restore(live, backup):
    """Make `live` equal to `backup`: drop files the backup lacks, copy every backup file back."""
    keep = manifest(backup)
    for rel in manifest(live):
        if rel not in keep:
            os.remove(os.path.join(live, rel))
    for rel in keep:
        dst = os.path.join(live, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        copy_atomic(os.path.join(backup, rel), dst)


def later_swaps(rid, live):
    """Runs swapped into the same memory folder after `rid` and not undone since (their live-backup still exists)."""
    swaps = [s for s in (load(STATE, {}).get('swaps') or []) if isinstance(s, dict)]
    last = max((i for i, s in enumerate(swaps) if s.get('run') == rid), default=None)
    if last is None:
        return []
    out = []
    for s in swaps[last + 1:]:
        r = s.get('run')
        if not r or r == rid or r in out or not os.path.isdir(os.path.join(run_dir(r), 'live-backup')):
            continue
        other = load(os.path.join(run_dir(r), 'run.json'), {})
        if os.path.realpath(other.get('memory_source') or LIVE_MEMORY) == os.path.realpath(live):
            out.append(r)
    return out


@locked
def cmd_undo(a):
    d = run_dir(a.run)
    run = load(os.path.join(d, 'run.json'), {})
    live = run.get('memory_source') or LIVE_MEMORY
    backup = os.path.join(d, 'live-backup')
    if not os.path.isdir(backup):
        return emit({'error': 'no live-backup for this run'}, 2)
    links = symlinks_in(live)
    if links:
        return emit({'error': 'the memory folder holds symlinks (%s); ONEIROS never writes through links' % ', '.join(links[:5])}, 2)
    later = later_swaps(a.run, live)
    if later:
        return emit({'error': 'run %s was swapped after %s; undo %s first (last swapped, first undone)' % (
            later[-1], a.run, ', then '.join(reversed(later)))}, 2)
    stamp = now().strftime('%Y%m%d%H%M%S')
    snap = fresh(os.path.join(d, 'live-before-undo-' + stamp))
    shutil.copytree(live, snap, symlinks=True)  # edits other sessions made after the swap stay recoverable from here
    rec = load(os.path.join(d, 'swap.json'), {})
    after, current = rec.get('after_manifest'), manifest(live)
    changed = sorted(k for k in set(current) | set(after or {}) if (after or {}).get(k) != current.get(k)) if after else None
    restore(live, backup)
    shutil.move(backup, fresh(backup + '-undone-' + stamp))
    if rec:
        save(os.path.join(d, 'swap.json'), dict(rec, status='undone', undone=iso(now()), live_before_undo=snap))
    return emit({'run': a.run, 'restored': True, 'live_before_undo': snap,
                 'changed_since_swap': changed if changed is not None else 'unknown (no swap record)'})


def cmd_dismiss(a):
    with state_update() as state:
        if state.get('pending_review') == a.run:
            state.pop('pending_review')
    path = os.path.join(run_dir(a.run), 'run.json')
    run = load(path, None)
    if isinstance(run, dict) and not run.get('eval') and run.get('status') != 'dismissed':
        save(path, dict(run, status='dismissed', dismissed=iso(now())))
    return emit({'run': a.run, 'dismissed': True})


def cmd_due(a):
    state = load(STATE, {})
    why = need_folder(a.project or PROJECT, 'Claude Code project')
    if why:
        return emit({'due': False, 'why': why}, 2)
    stopped = stopped_runs(state)
    if len(stopped) >= MAX_STOPPED_RUNS:
        return emit({'due': False, 'why': '%d dreams in a row stopped before their report (%s); dismiss them after a look'
                                         % (len(stopped), ', '.join(stopped))}, 2)
    if not state.get('last_finished') and not cursor_of(state.get('next_since')):
        return emit({'due': False, 'why': 'no dream has finished yet: run the first one by hand with start --since YYYY-MM-DD'}, 2)
    exclude = current_session_ids()
    sessions = [s for s in session_index(a.project or PROJECT) if s['sid'] not in exclude]
    ok, why = is_due(state, sessions)
    return emit({'due': ok, 'why': why}, 0 if ok else 3)


def cmd_clean_sandboxes(a):
    """List, or with --yes delete, the folders Claude Code left under ~/.claude/projects for past ONEIROS sandboxes."""
    found = sandbox_traces()
    deleted = 0
    if a.yes:
        for p in found:
            shutil.rmtree(p, ignore_errors=True)
            deleted += not os.path.exists(p)
    return emit({'folders': found, 'deleted': deleted})


def cmd_render(a):
    text, meta = render_session(a.transcript, cap=a.cap)
    sys.stdout.write(text)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='oneiros', description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('due'); s.add_argument('--project'); s.set_defaults(f=cmd_due)
    s = sub.add_parser('render'); s.add_argument('transcript'); s.add_argument('--cap', type=int, default=RENDER_CAP); s.set_defaults(f=cmd_render)
    s = sub.add_parser('start'); s.add_argument('--id'); s.add_argument('--since'); s.add_argument('--until')
    s.add_argument('--memory', help='memory folder to snapshot (default: live memory)'); s.add_argument('--project')
    s.add_argument('--exclude', nargs='*'); s.add_argument('--eval', action='store_true', help='evaluation run: no lock, no review state')
    s.add_argument('--after', help='chain after this gated run: snapshot its staged memory, swap into its memory source')
    s.set_defaults(f=cmd_start)
    for name, fn, model, effort, budget in (('analyze', cmd_analyze, 'sonnet', 'medium', 1.5), ('consolidate', cmd_consolidate, 'sonnet', 'medium', 6.0)):
        s = sub.add_parser(name); s.add_argument('run'); s.add_argument('--model', default=model); s.add_argument('--effort', default=effort)
        s.add_argument('--budget', type=float, default=budget); s.add_argument('--workers', type=int, default=4); s.add_argument('--today')
        s.set_defaults(f=fn)
    s = sub.add_parser('mechanical', help='control arm: append ops without a model call')
    s.add_argument('run'); s.add_argument('--from', dest='from_run', required=True, help='dream run to copy'); s.add_argument('--today')
    s.set_defaults(f=cmd_mechanical)
    s = sub.add_parser('apply'); s.add_argument('run'); s.add_argument('--today'); s.set_defaults(f=cmd_apply)
    s = sub.add_parser('gate'); s.add_argument('run'); s.set_defaults(f=cmd_gate)
    s = sub.add_parser('report'); s.add_argument('run'); s.add_argument('--notify', action='store_true'); s.set_defaults(f=cmd_report)
    s = sub.add_parser('swap'); s.add_argument('run')
    s.add_argument('--skip', help='operation numbers not to apply, as numbered under Applied in REPORT.md (for example 3,7)')
    s.set_defaults(f=cmd_swap)
    s = sub.add_parser('undo'); s.add_argument('run'); s.set_defaults(f=cmd_undo)
    s = sub.add_parser('dismiss'); s.add_argument('run'); s.set_defaults(f=cmd_dismiss)
    s = sub.add_parser('clean-sandboxes', help='list (or with --yes delete) folders past sandboxes left in ~/.claude/projects')
    s.add_argument('--yes', action='store_true'); s.set_defaults(f=cmd_clean_sandboxes)
    a = p.parse_args(argv)
    if CONFIG_ERROR and a.cmd != 'render':
        return emit({'error': CONFIG_ERROR}, 2)
    if not PROTECTED_CONFIGURED and a.cmd in ('due', 'start', 'apply', 'swap', 'undo'):
        return emit({'error': 'append-only files not configured: set "protected" in oneiros.config.json (an empty list [] '
                              'opts out) or ONEIROS_PROTECTED', 'due': False}, 2)
    for k in ('run', 'id', 'after', 'from_run'):
        v = getattr(a, k, None)
        if v is not None and not RUN_ID_RE.fullmatch(v):
            return emit({'error': 'bad run id %r: letters, digits, dot, underscore and hyphen only' % v}, 2)
    if a.cmd in ('analyze', 'consolidate', 'apply', 'gate', 'report', 'swap', 'undo') \
            and not os.path.isfile(os.path.join(run_dir(a.run), 'run.json')):
        return emit({'error': 'no run %s (no runs/%s/run.json)' % (a.run, a.run)}, 2)
    return a.f(a)


if __name__ == '__main__':
    sys.exit(main())
