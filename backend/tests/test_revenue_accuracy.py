"""Regression tests for the three revenue dashboard bugs.

Run from the backend container (no extra dependency needed):

    docker compose exec backend python -m unittest discover -s tests -v
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.services.cache import build_cache_key
from app.services.reservations import CENTS, _round_money, month_bounds_utc


class MonthBoundariesAreLocal(unittest.TestCase):
    """Bug 2: month bounds were built as naive (UTC) datetimes."""

    def test_paris_march_starts_one_hour_before_utc_midnight(self):
        start, end = month_bounds_utc(2024, 3, "Europe/Paris")
        self.assertEqual(start.astimezone(timezone.utc).isoformat(), "2024-02-29T23:00:00+00:00")
        self.assertEqual(end.astimezone(timezone.utc).isoformat(), "2024-03-31T22:00:00+00:00")

    def test_booking_at_2024_02_29_2330z_belongs_to_march_in_paris(self):
        # Seed row `res-tz-1`: 2024-02-29 23:30Z is 2024-03-01 00:30 in Paris.
        check_in = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)
        march_start, march_end = month_bounds_utc(2024, 3, "Europe/Paris")
        february_start, february_end = month_bounds_utc(2024, 2, "Europe/Paris")

        self.assertTrue(march_start <= check_in < march_end)
        self.assertFalse(february_start <= check_in < february_end)

    def test_same_booking_belongs_to_february_in_new_york(self):
        check_in = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)
        start, end = month_bounds_utc(2024, 2, "America/New_York")
        self.assertTrue(start <= check_in < end)

    def test_december_rolls_over_to_next_year(self):
        start, end = month_bounds_utc(2024, 12, "Europe/Paris")
        self.assertEqual(start.year, 2024)
        self.assertEqual((end.year, end.month), (2025, 1))

    def test_bounds_are_timezone_aware(self):
        start, end = month_bounds_utc(2024, 3, "Europe/Paris")
        self.assertIsNotNone(start.tzinfo)
        self.assertIsNotNone(end.tzinfo)


class MoneyIsRoundedOnce(unittest.TestCase):
    """Bug 3: totals drifted by a cent."""

    # prop-001 / tenant-a, stored as NUMERIC(10, 3).
    ROWS = [Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]

    def test_rounding_the_sum_keeps_the_cent(self):
        self.assertEqual(_round_money(sum(self.ROWS)), Decimal("1000.00"))

    def test_rounding_each_row_loses_a_cent(self):
        per_row = sum(row.quantize(CENTS, rounding=ROUND_HALF_UP) for row in self.ROWS)
        self.assertEqual(per_row, Decimal("999.99"))
        self.assertNotEqual(per_row, _round_money(sum(self.ROWS)))

    def test_result_always_has_two_decimals(self):
        for value in ("0", "1250", "1080.4", "333.333"):
            self.assertEqual(_round_money(Decimal(value)).as_tuple().exponent, -2)

    def test_half_up_not_bankers_rounding(self):
        # Decimal defaults to ROUND_HALF_EVEN, which would give 0.12 here.
        self.assertEqual(_round_money(Decimal("0.125")), Decimal("0.13"))


class CacheKeysAreTenantScoped(unittest.TestCase):
    """Bug 1: the cache key ignored the tenant."""

    def test_same_property_id_across_tenants_uses_different_keys(self):
        # prop-001 exists for both tenant-a and tenant-b (see database/seed.sql).
        self.assertNotEqual(
            build_cache_key("prop-001", "tenant-a"),
            build_cache_key("prop-001", "tenant-b"),
        )

    def test_key_contains_tenant_property_and_period(self):
        self.assertEqual(
            build_cache_key("prop-001", "tenant-a", month=3, year=2024),
            "revenue:tenant-a:prop-001:2024-03",
        )
        self.assertEqual(
            build_cache_key("prop-001", "tenant-a"),
            "revenue:tenant-a:prop-001:all-time",
        )

    def test_periods_do_not_share_a_key(self):
        self.assertNotEqual(
            build_cache_key("prop-001", "tenant-a", month=3, year=2024),
            build_cache_key("prop-001", "tenant-a", month=2, year=2024),
        )


if __name__ == "__main__":
    unittest.main()
