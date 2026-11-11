"""Shared display contract, independent of any installed CLI or login."""
from dataclasses import replace
import unittest

from usage_adapters import ProviderSpec, tile_pages, PROVIDERS
from usage_tiles import (ANSI, Billing, Status, UsageTile, UsageWindow, TokenSpend,
                         TileFact, render_tiles, tile_lines)
from usage import demo_usage
from quota import muse_allowance, grok_allowance


ACCENT = '\x1b[38;2;120;180;200m'


def plain(tile, width=80):
    return '\n'.join(ANSI.sub('', line) for line in render_tiles([tile], width, now=1000))


class SharedTileTests(unittest.TestCase):
    def test_detected_providers_pack_alphabetically_for_every_subset(self):
        snapshot = demo_usage(now=1000)
        names = sorted(snapshot, key=str.casefold)
        for mask in range(1 << len(names)):
            detected = [name for i, name in enumerate(names) if mask & (1 << i)]
            pages = tile_pages({name: snapshot[name] for name in detected}, tuple(reversed(PROVIDERS)))
            self.assertEqual([[tile.provider for tile in page] for page in pages],
                             [detected[i:i+2] for i in range(0, len(detected), 2)])

    def test_all_tiles_share_height_and_freshness_stays_at_bottom(self):
        pages = tile_pages(demo_usage(now=1000))
        tiles = [tile for page in pages for tile in page]
        tiles[0] = replace(tiles[0], error='A refresh error that must appear above the footer')
        for width in (60, 120):
            heights = []
            for tile in tiles:
                rows = render_tiles([tile], width, now=1060, sizing_tiles=tiles)
                heights.append(len(rows))
                self.assertIn('updated 1m ago', ANSI.sub('', rows[-2]))
            self.assertEqual(len(set(heights)), 1)

    def test_three_modes_share_one_renderer_for_an_unregistered_provider(self):
        spec = ProviderSpec('Example CLI', ACCENT)
        snapshots = [
            dict(subscription_status='inactive'),
            dict(billing_mode='subscription', windows=[dict(label='5h', usedPercent=25, resetsAt=4600),
                                                       dict(label='week', usedPercent=90, resetsAt=9000)]),
            dict(billing_mode='token', token_spend=dict(tokens=12500, cost_usd=.02, scope='Current session')),
        ]
        tiles = [spec.adapter(spec, dict(data=data, updated_at=900)) for data in snapshots]
        self.assertEqual([tile.status for tile in tiles], [Status.NO_SUBSCRIPTION, Status.READY, Status.READY])
        self.assertIn('No active subscription allowance.', plain(tiles[0]))
        self.assertIn('25% used', plain(tiles[1]))
        self.assertIn('75% left', plain(tiles[1]))
        self.assertIn('90% used', plain(tiles[1]))
        self.assertIn('Tokens spent: 12,500', plain(tiles[2]))
        self.assertIn('Cost: $0.0200', plain(tiles[2]))
        for tile in tiles:
            for width in (12, 24, 40, 80, 110, 160):
                lines = render_tiles([tile], width, now=1000)
                self.assertLessEqual(max(len(ANSI.sub('', line)) for line in lines), width)

    def test_missing_and_failed_data_never_claim_no_subscription(self):
        spec = ProviderSpec('Example', ACCENT)
        for state, expected in [(dict(detected=True), Status.LOADING),
                                (dict(detected=True, error='refresh failed'), Status.UNAVAILABLE),
                                (dict(data=dict(windows=[dict(label='week')])), Status.UNAVAILABLE)]:
            tile = spec.adapter(spec, state)
            self.assertEqual(tile.status, expected)
            self.assertNotIn('No active subscription', plain(tile))
            self.assertNotIn('0% used', plain(tile))

    def test_only_explicit_token_billing_can_display_token_spend(self):
        spec = ProviderSpec('Example', ACCENT)
        for mode in ('subscription', 'unknown', None):
            tile = spec.adapter(spec, dict(data=dict(billing_mode=mode,
                token_spend=dict(tokens=9000, cost_usd=20))))
            self.assertIsNone(tile.spend)
            self.assertNotIn('Tokens spent', plain(tile))
            self.assertNotIn('$20', plain(tile))
        with self.assertRaises(ValueError):
            UsageTile('Example', ACCENT, billing=Billing.SUBSCRIPTION, spend=TokenSpend(tokens=100))

    def test_reported_zero_is_distinct_from_unknown_token_spend(self):
        spec = ProviderSpec('Example', ACCENT)
        unknown = spec.adapter(spec, dict(data=dict(billing_mode='token')))
        zero = spec.adapter(spec, dict(data=dict(billing_mode='token', token_spend=dict(tokens=0, cost_usd=0))))
        self.assertIn('Token spend unavailable', plain(unknown))
        self.assertNotIn('$0', plain(unknown))
        self.assertIn('Tokens spent: 0', plain(zero))
        self.assertIn('Cost: $0.0000', plain(zero))

    def test_estimate_scope_freshness_error_and_credit_metadata_survive(self):
        tile = UsageTile('Example', ACCENT, billing=Billing.TOKEN, status=Status.READY,
            spend=TokenSpend(tokens=12500, cost_usd=.034, estimated=True, scope='Latest session only'),
            updated_at=900, error='refresh failed', facts=(
                TileFact('Prepaid credits', '$12.50'), TileFact('Reset credit', 'Available', 'highlight', 4600)))
        text = plain(tile)
        for value in ('Latest session only', 'Estimated cost: $0.0340', 'Prepaid credits: $12.50',
                      'RESET CREDIT: Available', 'expires in 1h 0m', 'updated 2m ago', 'refresh failed'):
            self.assertIn(value, text)

    def test_unknown_percentage_preserves_reported_reset(self):
        tile = UsageTile('Example', ACCENT, windows=(UsageWindow('week', resets_at=4600),))
        self.assertIn('week: unavailable', plain(tile))
        self.assertIn('reset in 1h 0m', plain(tile))

    def test_missing_subscription_clears_inconsistent_old_quota(self):
        spec = ProviderSpec('Example', ACCENT)
        tile = spec.adapter(spec, dict(data=dict(subscription_status='inactive',
            windows=[dict(label='week', usedPercent=50)])))
        self.assertEqual(tile.windows, ())
        self.assertNotIn('50% used', plain(tile))

    def test_malformed_and_nonfinite_values_never_make_a_meter(self):
        spec = ProviderSpec('Example', ACCENT)
        for value in (True, -1, float('nan'), float('inf'), '50'):
            tile = spec.adapter(spec, dict(data=dict(windows=[dict(label='week', usedPercent=value)])))
            self.assertIn('week: unavailable', plain(tile))
            self.assertNotIn('% used', plain(tile))
        self.assertEqual(spec.adapter(spec, dict(data=dict(windows=42))).windows, ())

    def test_new_providers_paginate_without_renderer_changes(self):
        specs = tuple(ProviderSpec(name, ACCENT) for name in ('New A', 'New B', 'New C'))
        snapshot = {spec.name: dict(data=dict(billing_mode='token')) for spec in specs}
        pages = tile_pages(snapshot, specs)
        self.assertEqual([[tile.provider for tile in page] for page in pages], [['New A', 'New B'], ['New C']])
        for page in pages:
            lines = render_tiles(page, 120, now=1000)
            self.assertLessEqual(max(len(ANSI.sub('', line)) for line in lines), 120)

    def test_broken_adapter_does_not_remove_other_tiles(self):
        def broken(*_):
            raise ValueError('unexpected payload with private data')
        specs = (ProviderSpec('Broken', ACCENT, adapter=broken), ProviderSpec('Healthy', ACCENT))
        pages = tile_pages({spec.name: dict(detected=True) for spec in specs}, specs)
        bad, good = pages[0]
        self.assertEqual(bad.status, Status.UNAVAILABLE)
        self.assertIn('format not recognized', bad.error)
        self.assertNotIn('private data', plain(bad))
        self.assertEqual(good.status, Status.LOADING)

    def test_text_content_does_not_control_styles(self):
        tile = UsageTile('Example', ACCENT, status=Status.READY,
                         facts=(TileFact('Note', 'quota reached text is only a note'),))
        self.assertNotIn('\x1b[38;2;255;112;125m', '\n'.join(render_tiles([tile], 80)))

    def test_live_response_shapes_and_demo_cover_all_modes(self):
        specs = {spec.name: spec for spec in PROVIDERS}
        no_allowance = specs['Muse'].adapter(specs['Muse'], dict(data=muse_allowance(
            dict(allowance=None, reason='no_allowance'))))
        failed_read = specs['Muse'].adapter(specs['Muse'], dict(data=muse_allowance(
            dict(allowance=None, reason='server_sent_no_usage'))))
        free = specs['Grok'].adapter(specs['Grok'], dict(data=grok_allowance(dict(
            subscription_tier='Free', config=dict(currentPeriod=dict(type='USAGE_PERIOD_TYPE_WEEKLY'))))))
        self.assertEqual(no_allowance.status, Status.NO_SUBSCRIPTION)
        self.assertEqual(failed_read.status, Status.UNAVAILABLE)
        self.assertEqual(free.status, Status.UNAVAILABLE)
        tiles = [tile for page in tile_pages(demo_usage(now=1000)) for tile in page]
        self.assertTrue(any(tile.status == Status.NO_SUBSCRIPTION for tile in tiles))
        self.assertTrue(any(tile.billing == Billing.TOKEN and tile.spend for tile in tiles))
        self.assertTrue(any(tile.windows for tile in tiles))


if __name__ == '__main__':
    unittest.main()
