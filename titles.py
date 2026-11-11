"""Offline task titles: extract the request's core instruction without a model call.

A chat's first substantive prompt usually states its objective, but often after
scene-setting ("This is an asset pipeline to Unity."). Each sentence is scored:
explicit requests and imperatives beat questions, which beat context. The best
sentence loses greetings, politeness wrappers, code, URLs, and long paths, and
is trimmed at a word boundary.
"""
import re

FENCED = re.compile(r'```.*?(?:```|\Z)', re.S)
TAG = re.compile(r'<[^<>\n]{1,200}>')
URL = re.compile(r'\bhttps?://([^/\s]+)\S*', re.I)
# Windows and POSIX paths with at least one separator: keep the final name.
PATH = re.compile(r'(?:[A-Za-z]:)?(?:[\\/][^\\/\s"\'`<>|]+){2,}[\\/]?|(?:[A-Za-z]:)[\\/][^\s"\'`<>|]+')
INLINE_CODE = re.compile(r'`([^`\n]{1,60})`|`[^`\n]*`')
SENTENCE = re.compile(r'(?<=[.!?;:])\s+|\n+')
# Leading filler, repeated: "ok so hey, please can you ..." -> "...".
FILLER = re.compile(r"^(?:(?:hey|hi|hello|yo|ok(?:ay)?|so|alright|right|great|cool|nice|thanks?(?: you)?|"
                    r"please|pls|now|also|and|then|next|first|um+|uh+|well|quick question|basically)\b[\s,!.:;-]*)+", re.I)
REQUEST = re.compile(r"^(?:(?:can|could|would|will) you(?: please)?(?: help me)?|"
                     r"i(?:'d| would) (?:like|love) (?:for )?(?:you )?to|i (?:want|need) (?:you )?to|"
                     r"i'm (?:trying|hoping|looking) to|help me(?: to)?|let'?s|"
                     r"(?:i think )?we (?:need|should|have|ought) to|(?:i think )?we should|"
                     r"you (?:should|need to|must)|go ahead and|try to|"
                     r"your (?:only |main )?(?:job|task|goal) (?:here )?is (?:only |just )?to|"
                     r"the (?:goal|task) is to)\s+", re.I)
# Sentences that carry no task on their own.
CHATTER = re.compile(r"^(?:thanks?(?: you)?|thank you.*|ok(?:ay)?|yes|no|yep|nope|sure|great|perfect|nice|cool|"
                     r"(?:hey|hi|hello)(?: there)?|good (?:morning|afternoon|evening)|continue|go on|done|"
                     r"do the following|(?:here(?: is|'s)|see) (?:the|my) .{0,20}|you (?:just )?have one task)\W*$", re.I)
# Scene-setting rather than asking: "This directory holds ...", "I renamed ...".
CONTEXT = re.compile(r"^(?:this|that|these|those|there|here|our|my|it|its|currently|right now|"
                     r"at the moment|so far|previously|earlier|recently|i|i've|i'm|i'd|we|we've|we're)\b", re.I)
VERBS = set('''add adjust align analyze animate audit build change check clean compare configure convert
create debug delete deploy design document draft explain export extend find finish fix generate get give
implement import improve increase install investigate list look make merge migrate move optimize organize
plan port profile refactor reduce remove rename render reorganize replace research resume revert review
revisit rewrite rotate run scale send set show simplify speed split start summarize support take test
tidy translate tune turn update upgrade use validate verify write'''.split())
TRAILING = {'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'into', 'of', 'on', 'or',
            'that', 'the', 'to', 'with', 'which', 'while', 'if', 'so'}


def simplify(text):
    text = FENCED.sub(' ', text)
    text = TAG.sub(' ', text)
    text = URL.sub(lambda m: m[1], text)
    text = PATH.sub(lambda m: re.split(r'[\\/]', m[0].rstrip('\\/'))[-1], text)
    text = INLINE_CODE.sub(lambda m: m[1] or ' ', text)
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text)


def core(sentence):
    """Return the sentence without wrappers, and whether it was phrased as a request."""
    previous, requested = None, False
    while previous != sentence:  # Strip "ok so can you please ..." in any order.
        previous = sentence
        sentence = FILLER.sub('', sentence.strip())
        stripped = REQUEST.sub('', sentence)
        requested |= stripped != sentence
        sentence = stripped.strip()
    sentence = re.sub(r'\s+', ' ', sentence).strip(' \t,:;-–—"\'')
    return sentence.rstrip('.!:;') if not sentence.endswith('?') else sentence[:-1].rstrip(), requested


def score(original, candidate, requested):
    first = candidate.split(' ', 1)[0].lower()
    if requested or first in VERBS:
        return 3
    if original.rstrip().endswith('?'):
        return 2
    return 0 if CONTEXT.match(candidate) else 1


def shorten(text, width):
    if len(text) <= width:
        return text
    words = text[:width].rsplit(' ', 1)[0].rstrip(' ,;:-–—').split(' ')
    while len(words) > 1 and words[-1].lower().strip(',') in TRAILING:
        words.pop()
    return ' '.join(words).rstrip(' ,;:-–—') + '…'


def request_title(text, width=80, sentences=8):
    """Title for one prompt, or '' when it has no substantive request."""
    if not isinstance(text, str):
        return ''
    best = None
    for index, sentence in enumerate(s for s in SENTENCE.split(simplify(text)) if s.strip()):
        if index >= sentences:
            break
        candidate, requested = core(sentence)
        if len(re.findall(r'\w+', candidate)) < 2 or CHATTER.match(candidate):
            continue
        rank = score(sentence, candidate, requested)
        if best is None or rank > best[0]:
            best = rank, candidate
        if rank == 3:
            break  # The earliest explicit request states the objective.
    if best is None:
        return ''
    return shorten(best[1][0].upper() + best[1][1:], width)


def heuristic_title(prompts, width=80):
    """Title from the first prompt that states a task; else the first prompt itself."""
    prompts = [p for p in prompts if isinstance(p, str) and p.strip()]
    for prompt in prompts:
        title = request_title(prompt, width)
        if title:
            return title
    if prompts:
        text = re.sub(r'\s+', ' ', simplify(prompts[0])).strip()
        return shorten(text, width) if text else ''
    return ''
