"""Provider-independent usage tiles. Adapters supply data; this module draws it.

New agents reuse UsageTile, UsageWindow, TokenSpend and TileFact. Rendering
never inspects provider payloads or reparses formatted prose.
"""
from dataclasses import dataclass
from enum import StrEnum
import math
import re
import textwrap
import time


class Billing(StrEnum):
    UNKNOWN = 'unknown'
    SUBSCRIPTION = 'subscription'
    TOKEN = 'token'


class Status(StrEnum):
    READY = 'ready'
    NO_SUBSCRIPTION = 'no_subscription'
    LOADING = 'loading'
    UNAVAILABLE = 'unavailable'


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used_percent: float | None = None
    resets_at: float | None = None
    disabled: bool = False


@dataclass(frozen=True)
class TokenSpend:
    tokens: int | None = None
    cost_usd: float | None = None
    estimated: bool = False
    scope: str = 'Reported usage'


@dataclass(frozen=True)
class TileFact:
    label: str
    value: str
    tone: str = 'muted'
    expires_at: float | None = None


@dataclass(frozen=True)
class UsageTile:
    provider: str
    accent: str
    billing: Billing = Billing.UNKNOWN
    status: Status = Status.UNAVAILABLE
    plan: str = ''
    windows: tuple[UsageWindow, ...] = ()
    spend: TokenSpend | None = None
    facts: tuple[TileFact, ...] = ()
    updated_at: float | None = None
    error: str = ''
    message: str = ''

    def __post_init__(self):
        if self.spend is not None and self.billing != Billing.TOKEN:
            raise ValueError('Token spend requires explicit token billing')


ESC = '\x1b['
RESET = ESC + '0m'
WHITE = ESC + '38;2;235;241;250m'
MUTED = ESC + '38;2;158;176;198m'
TRACK = ESC + '38;2;56;72;94m'
GOLD = ESC + '38;2;255;202;104m'
RED = ESC + '38;2;255;112;125m'
MINT = ESC + '38;2;112;224;185m'
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def duration(seconds):
    minutes = max(0, math.ceil(seconds / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return (f'{days}d ' if days else '') + (f'{hours}h ' if hours or days else '') + f'{minutes}m'


def reset_text(timestamp, now):
    if not finite(timestamp):
        return 'reset unknown'
    try:
        local = time.strftime('%a %H:%M', time.localtime(timestamp))
    except (ValueError, OverflowError, OSError):
        return 'reset unknown'
    return ('awaiting refresh' if timestamp <= now else 'in ' + duration(timestamp-now)) + f' ({local})'


def fresh_text(tile, now):
    return 'updated ' + (duration(now-tile.updated_at) + ' ago' if finite(tile.updated_at) and tile.updated_at > 0 else 'never')


def status_text(tile):
    return tile.message or {
        Status.NO_SUBSCRIPTION: 'No active subscription allowance.',
        Status.LOADING: 'Fetching account limits...',
        Status.UNAVAILABLE: 'Usage unavailable',
    }.get(tile.status, '')


def spend_lines(spend):
    if spend is None:
        return ['Token spend unavailable']
    lines = [spend.scope]
    if finite(spend.tokens) and spend.tokens >= 0:
        lines.append(f'Tokens spent: {spend.tokens:,.0f}')
    if finite(spend.cost_usd) and spend.cost_usd >= 0:
        lines.append(f'{"Estimated cost" if spend.estimated else "Cost"}: ${spend.cost_usd:.4f}')
    if len(lines) == 1:
        lines.append('Token spend unavailable')
    return lines


def tile_lines(tile, now=None):
    """Plain-text view of the same model, for terminal integrations and tests."""
    now = time.time() if now is None else now
    lines = [tile.provider + ' usage | ' + fresh_text(tile, now)]
    if tile.plan:
        lines.append('  plan: ' + tile.plan)
    if status_text(tile):
        lines.append('  ' + status_text(tile))
    for window in tile.windows:
        if window.disabled:
            text = window.label + ': disabled'
        elif finite(window.used_percent) and window.used_percent >= 0:
            percent = window.used_percent
            filled = round(min(100, percent)/10)
            text = f'{window.label} [{"#"*filled}{"-"*(10-filled)}] {percent:g}% used ({max(0, 100-percent):g}% left) | resets {reset_text(window.resets_at, now)}'
        else:
            text = window.label + ': unavailable | resets ' + reset_text(window.resets_at, now)
        lines.append('  ' + text)
    if tile.billing == Billing.TOKEN:
        lines.extend('  ' + line for line in spend_lines(tile.spend))
    for fact in tile.facts:
        text = (fact.label + ': ' if fact.label else '') + fact.value
        if fact.expires_at is not None:
            text += ' | expires ' + reset_text(fact.expires_at, now)
        lines.append('  ' + text)
    if tile.error:
        lines.append('  ' + tile.error)
    return lines


def render_tiles(tiles, width, now=None, sizing_tiles=None):
    """Shared layout for any provider; all provider-specific logic stays in adapters."""
    now = time.time() if now is None else now
    if not tiles or width < 8:
        return []
    sizing_tiles = sizing_tiles or tiles
    columns = 2 if len(sizing_tiles) > 1 and width >= 110 else 1
    card_width = (width-5)//2 if columns == 2 else width-2
    inside = card_width-4

    def safe(text):
        return re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(text))

    def card(tile):
        accent, rows = tile.accent, []
        def row(text='', style=WHITE):
            visible = len(ANSI.sub('', text))
            rows.append(accent + '│ ' + style + text + ' '*max(0, inside-visible) + RESET + accent + ' │' + RESET)
        def wrapped(text, style=MUTED):
            for part in textwrap.wrap(safe(text), width=inside) or ['']:
                row(part, style)
        title = (' ' + safe(tile.provider).upper() + ' / USAGE ')[:card_width-3]
        rows.append(accent + '╭─' + ESC+'1m' + title + RESET + accent + '─'*max(0, card_width-len(title)-3) + '╮' + RESET)
        if tile.plan:
            wrapped('PLAN ' + tile.plan, accent)
        if status_text(tile):
            wrapped(status_text(tile), GOLD if tile.status == Status.UNAVAILABLE else MUTED)
        for window in tile.windows:
            label = safe(window.label)
            if window.disabled:
                wrapped(label + ': disabled')
                continue
            if not finite(window.used_percent) or window.used_percent < 0:
                wrapped(label + ': unavailable')
                if finite(window.resets_at):
                    wrapped('↳ reset ' + reset_text(window.resets_at, now), WHITE)
                continue
            percent = window.used_percent
            ink = RED if percent >= 90 else GOLD if percent >= 70 else accent
            value = f'{percent:g}'
            cells = max(4, min(18, inside-len(label)-len(value)-12))
            units = round(min(100, percent)/100*cells*8)
            full, fraction = divmod(units, 8)
            bar = '█'*full + ('▏▎▍▌▋▊▉'[fraction-1] if fraction else '')
            meter = ink + bar + TRACK + '░'*(cells-full-bool(fraction)) + RESET
            summary = f'{label.upper():<5} ' + meter + ink + ESC+'1m' + f' {value}% used' + RESET
            if len(ANSI.sub('', summary)) <= inside:
                row(summary)
            else:
                wrapped(f'{label.upper()} {value}% used', ink)
            wrapped(f'↳ {max(0, 100-percent):g}% left · reset ' + reset_text(window.resets_at, now), WHITE)
        if tile.billing == Billing.TOKEN:
            for line in spend_lines(tile.spend):
                wrapped(line, WHITE)
        for fact in tile.facts:
            label = fact.label.upper() if fact.tone == 'highlight' else fact.label
            text = (label + ': ' if label else '') + fact.value
            if fact.expires_at is not None:
                text += ' · expires ' + reset_text(fact.expires_at, now)
            wrapped(text, {'warning': GOLD, 'danger': RED, 'highlight': MINT, 'accent': accent}.get(fact.tone, MUTED))
        if tile.error:
            wrapped('! ' + tile.error, GOLD)
        footer_start = len(rows)
        row('─'*inside, TRACK)
        wrapped('◷ ' + fresh_text(tile, now))
        rows.append(accent + '╰' + '─'*(card_width-2) + '╯' + RESET)
        return rows, footer_start

    height = max(len(card(tile)[0]) for tile in sizing_tiles)
    cards = []
    for tile in tiles:
        rows, footer_start = card(tile)
        blank = tile.accent + '│' + ' '*(card_width-2) + '│' + RESET
        rows[footer_start:footer_start] = [blank] * max(0, height-len(rows))
        cards.append(rows)
    if columns == 1 or len(cards) == 1:
        return [' ' + line for rows in cards for line in rows]
    return [' ' + left + '   ' + right for left, right in zip(*cards)]
