"""Unit tests for the in-memory TTL cache and the phone helpers."""

import pytest
from src.core.phone import mask_phone, normalize_phone, phone_key
from src.infrastructure.cache.ttl_cache import TTLCache
from tests.fakes import FakeClock


def test_entry_expires_after_ttl():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=60, clock=clock)
    cache.set("a", 1)

    clock.advance(59)
    assert cache.get("a") == 1
    clock.advance(1)
    assert cache.get("a") is None
    assert cache.get("a", default="missing") == "missing"


def test_falsy_values_are_cached_and_distinguishable_from_missing():
    cache = TTLCache(ttl_seconds=60, clock=FakeClock())
    cache.set("empty", {})
    assert cache.get("empty") == {}
    assert cache.get("other") is None


def test_stale_entries_can_be_served_for_a_grace_period():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=60, clock=clock)
    cache.set("a", "old")

    clock.advance(100)
    assert cache.get("a") is None
    assert cache.get("a", max_stale=50) == "old"
    assert cache.get("a", max_stale=30) is None


def test_set_if_absent_is_atomic_and_respects_expiry():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=10, clock=clock)

    assert cache.set_if_absent("msg", True) is True
    assert cache.set_if_absent("msg", True) is False
    clock.advance(11)
    assert cache.set_if_absent("msg", True) is True


def test_delete_removes_entry():
    cache = TTLCache(ttl_seconds=60, clock=FakeClock())
    cache.set("a", 1)
    cache.delete("a")
    cache.delete("never-set")
    assert cache.get("a") is None


def test_capacity_drops_expired_then_oldest_entries():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=10, max_entries=3, clock=clock)
    cache.set("old", 1)
    clock.advance(11)  # "old" is now expired
    cache.set("b", 2)
    cache.set("c", 3)
    cache.set("d", 4)  # over capacity: the expired entry goes first
    assert cache.get("old", max_stale=100) is None
    assert [cache.get(k) for k in ("b", "c", "d")] == [2, 3, 4]

    cache.set("e", 5)  # still over capacity with nothing expired: oldest write is dropped
    assert cache.get("b") is None
    assert [cache.get(k) for k in ("c", "d", "e")] == [3, 4, 5]


def test_rewriting_a_key_refreshes_its_ttl():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=10, clock=clock)
    cache.set("a", 1)
    clock.advance(8)
    cache.set("a", 2)
    clock.advance(8)
    assert cache.get("a") == 2


@pytest.mark.parametrize(
    "raw, digits",
    [
        ("+54 9 11 2233-4455", "5491122334455"),
        (5491122334455, "5491122334455"),
        (5491122334455.0, "5491122334455"),
        ("5491122334455@s.whatsapp.net", "5491122334455"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_phone(raw, digits):
    assert normalize_phone(raw) == digits


def test_phone_key_matches_argentine_mobile_with_and_without_the_nine():
    assert phone_key("5491122334455") == phone_key("541122334455") == "541122334455"
    assert phone_key("+54 9 11 2233-4455") == "541122334455"
    # Other countries are left untouched
    assert phone_key("5511987654321") == "5511987654321"


def test_mask_phone_keeps_prefix_and_last_four_digits():
    assert mask_phone("5491122334455") == "549****4455"
    assert mask_phone("1234") == "****"
