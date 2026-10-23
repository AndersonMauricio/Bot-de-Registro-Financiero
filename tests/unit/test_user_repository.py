"""Unit tests for the Google Sheets users repository: states, in-memory cache and writes."""

import pytest
from gspread.utils import ValueInputOption
from src.core.exceptions import StorageError
from src.domain.enums import UserStatusEnum
from src.domain.models import OnboardingData
from src.infrastructure.storage import sheets_users_repo
from src.infrastructure.storage.sheets_users_repo import GoogleSheetsUserRepository, HEADERS
from tests.fakes import FakeClock, FakeWorksheet

PHONE_APPROVED = "5491100000001"
PHONE_REJECTED = "5491100000002"
PHONE_PENDING = "5491100000003"
PHONE_UNKNOWN = "5491100000009"


def sheet(*rows) -> FakeWorksheet:
    return FakeWorksheet([HEADERS, *rows])


def make_repo(ws: FakeWorksheet, clock: FakeClock = None) -> GoogleSheetsUserRepository:
    return GoogleSheetsUserRepository(worksheet=ws, clock=clock or FakeClock())


# ---------------------------------------------------------------- the three states
def test_three_states_are_read_from_the_estado_column():
    repo = make_repo(sheet(
        [PHONE_APPROVED, 1, "Ana Prueba", "ana@example.com", "", ""],
        [PHONE_REJECTED, 2, "Beto Prueba", "beto@example.com", "", ""],
        [PHONE_PENDING, "", "Carla Prueba", "carla@example.com", "", ""],
    ))

    assert repo.get_user(PHONE_APPROVED).status == UserStatusEnum.APPROVED
    assert repo.get_user(PHONE_REJECTED).status == UserStatusEnum.REJECTED
    pending = repo.get_user(PHONE_PENDING)
    assert pending is not None and pending.status is None
    assert pending.full_name == "Carla Prueba"
    assert repo.get_user(PHONE_UNKNOWN) is None


@pytest.mark.parametrize("raw", [1, 1.0, "1", " 1 "])
def test_approved_accepts_the_ways_sheets_returns_a_one(raw):
    repo = make_repo(sheet([PHONE_APPROVED, raw, "", "", "", ""]))
    assert repo.get_user(PHONE_APPROVED).status == UserStatusEnum.APPROVED


@pytest.mark.parametrize("raw", [3, 0, -1, "aprobado", "yes", "true", True, "1,5", "12"])
def test_anything_other_than_1_or_2_is_never_approved(raw):
    """Strict validation: garbage in the estado cell must not grant (or deny) access."""
    repo = make_repo(sheet([PHONE_APPROVED, raw, "", "", "", ""]))
    assert repo.get_user(PHONE_APPROVED).status is None


def test_phone_cells_returned_as_numbers_are_matched():
    repo = make_repo(sheet([5491100000001.0, 1, "", "", "", ""], [5491100000002, 2, "", "", "", ""]))
    assert repo.get_user(PHONE_APPROVED).status == UserStatusEnum.APPROVED
    assert repo.get_user(PHONE_REJECTED).status == UserStatusEnum.REJECTED


def test_phones_match_regardless_of_formatting_and_the_argentine_mobile_nine():
    repo = make_repo(sheet(["+54 9 11 0000-0001", 1, "", "", "", ""]))
    assert repo.get_user("5491100000001").status == UserStatusEnum.APPROVED
    assert repo.get_user("541100000001").status == UserStatusEnum.APPROVED


def test_duplicated_rows_resolve_to_the_most_restrictive_explicit_status_whatever_their_order():
    other_pending = "5491100000004"
    repo = make_repo(sheet(
        [PHONE_APPROVED, 1, "", "", "", ""],
        [PHONE_APPROVED, 2, "", "", "", ""],  # rejection after approval
        [PHONE_REJECTED, 2, "", "", "", ""],
        [PHONE_REJECTED, 1, "", "", "", ""],  # approval after rejection: the rejection still wins
        [PHONE_PENDING, "", "", "", "", ""],
        [PHONE_PENDING, 1, "", "", "", ""],  # approval after an empty row
        [other_pending, 1, "", "", "", ""],
        [other_pending, "", "", "", "", ""],  # empty row after an approval: still approved
    ))
    assert repo.get_user(PHONE_APPROVED).status == UserStatusEnum.REJECTED
    assert repo.get_user(PHONE_REJECTED).status == UserStatusEnum.REJECTED
    assert repo.get_user(PHONE_PENDING).status == UserStatusEnum.APPROVED
    assert repo.get_user(other_pending).status == UserStatusEnum.APPROVED


def test_columns_may_be_reordered_and_extra_columns_ignored():
    ws = FakeWorksheet([
        ["notas", "estado", "email", "telefono"],
        ["vip", 1, "ana@example.com", PHONE_APPROVED],
    ])
    user = make_repo(ws).get_user(PHONE_APPROVED)
    assert user.status == UserStatusEnum.APPROVED
    assert user.email == "ana@example.com"


def test_missing_required_columns_is_an_error_not_an_empty_list_of_users():
    """Everyone silently becoming 'unknown' (and re-onboarded) would be worse than failing loudly."""
    repo = make_repo(FakeWorksheet([["telefono", "nombre"], [PHONE_APPROVED, "Ana"]]))
    with pytest.raises(StorageError, match="estado"):
        repo.get_user(PHONE_APPROVED)


def test_empty_worksheet_gets_its_headers_initialized():
    ws = FakeWorksheet([])
    repo = make_repo(ws)
    assert repo.get_user(PHONE_UNKNOWN) is None
    assert ws.appends[0][0] == HEADERS


# ------------------------------------------------------------------ cache behaviour
def test_one_sheet_read_serves_every_lookup_including_unknown_numbers():
    """Quota protection: unknown numbers must not trigger a read each."""
    ws = sheet([PHONE_APPROVED, 1, "", "", "", ""])
    repo = make_repo(ws)

    for i in range(50):
        repo.get_user(PHONE_APPROVED)
        repo.get_user(f"54911000{i:05d}")  # a different unknown number every time

    assert ws.reads == 1


def test_snapshot_is_refreshed_after_the_ttl_and_picks_up_manual_approval():
    clock = FakeClock()
    ws = sheet([PHONE_PENDING, "", "Carla", "carla@example.com", "", ""])
    repo = make_repo(ws, clock)
    assert repo.get_user(PHONE_PENDING).status is None

    ws.rows[1][1] = 1  # the administrator approves the user in the sheet
    clock.advance(59)
    assert repo.get_user(PHONE_PENDING).status is None  # still within the TTL
    assert ws.reads == 1

    clock.advance(2)
    assert repo.get_user(PHONE_PENDING).status == UserStatusEnum.APPROVED
    assert ws.reads == 2


def test_last_known_users_are_served_while_the_sheet_fails_then_access_fails_closed():
    clock = FakeClock()
    ws = sheet([PHONE_APPROVED, 1, "", "", "", ""])
    repo = make_repo(ws, clock)
    assert repo.get_user(PHONE_APPROVED).status == UserStatusEnum.APPROVED  # read #1

    ws.read_error = RuntimeError("429 quota exceeded")
    clock.advance(61)  # snapshot expired
    assert repo.get_user(PHONE_APPROVED).status == UserStatusEnum.APPROVED  # stale, read #2 failed
    assert ws.reads == 2

    # During the back-off the API is left alone
    for _ in range(10):
        repo.get_user(PHONE_APPROVED)
    assert ws.reads == 2

    clock.advance(sheets_users_repo.RETRY_BACKOFF_SECONDS + 1)
    repo.get_user(PHONE_APPROVED)
    assert ws.reads == 3  # retried once after the back-off, still served from memory

    clock.advance(sheets_users_repo.STALE_GRACE_SECONDS)  # too old to trust
    with pytest.raises(StorageError):
        repo.get_user(PHONE_APPROVED)


def test_without_any_snapshot_a_failing_sheet_fails_closed_and_is_not_hammered():
    ws = sheet()
    ws.read_error = RuntimeError("503")
    repo = make_repo(ws)

    with pytest.raises(StorageError):
        repo.get_user(PHONE_APPROVED)
    for _ in range(5):
        with pytest.raises(StorageError):
            repo.get_user(PHONE_APPROVED)

    assert ws.reads == 1


# ----------------------------------------------------------------------- writes
def test_new_user_is_appended_following_the_sheets_column_order_with_empty_estado():
    ws = FakeWorksheet([["email", "telefono", "notas", "estado", "nombre_completo", "fecha_alta", "fecha_actualizacion"]])
    repo = make_repo(ws)

    record = repo.save_onboarding(PHONE_UNKNOWN, OnboardingData(full_name="Dana Prueba", email="dana@example.com"))

    values, input_option = ws.appends[0]
    assert values[:5] == ["dana@example.com", PHONE_UNKNOWN, "", "", "Dana Prueba"]
    assert values[5] == values[6] != ""  # alta == actualizacion on creation
    assert input_option == ValueInputOption.raw  # user text is never interpreted as a formula
    assert record.status is None


def test_saving_onboarding_updates_the_cache_without_extra_reads():
    ws = sheet()
    repo = make_repo(ws)

    repo.save_onboarding(PHONE_UNKNOWN, OnboardingData(full_name="Dana Prueba"))
    reads_after_save = ws.reads
    user = repo.get_user(PHONE_UNKNOWN)

    assert (user.status, user.full_name) == (None, "Dana Prueba")
    assert ws.reads == reads_after_save


def test_existing_user_is_updated_in_place_and_estado_is_never_touched():
    ws = sheet([PHONE_PENDING, 1, "Carla", "", "2026-10-01 10:00:00", "2026-10-01 10:00:00"])
    repo = make_repo(ws)

    repo.save_onboarding(PHONE_PENDING, OnboardingData(full_name="Carla", email="carla@example.com"))

    assert ws.appends == []
    row = ws.rows[1]
    assert row[1] == 1  # estado untouched
    assert row[3] == "carla@example.com"
    assert row[4] == "2026-10-01 10:00:00"  # fecha_alta untouched
    assert row[5] != "2026-10-01 10:00:00"  # fecha_actualizacion refreshed
    assert len(ws.batch_updates) == 1  # a single write request


def test_updates_use_a_fresh_read_so_moved_rows_are_not_overwritten():
    """Cached row numbers must never be trusted: the sheet may have been edited since the last read."""
    ws = sheet([PHONE_PENDING, "", "Carla", "", "", ""], [PHONE_UNKNOWN, "", "Otra Persona", "otra@example.com", "", ""])
    repo = make_repo(ws)
    repo.get_user(PHONE_PENDING)  # warm the cache

    ws.rows.insert(1, ["5491100000077", "", "Intruso", "x@example.com", "", ""])  # admin inserts a row above
    repo.save_onboarding(PHONE_PENDING, OnboardingData(full_name="Carla", email="carla@example.com"))

    by_phone = {str(r[0]): r for r in ws.rows[1:]}
    assert by_phone[PHONE_PENDING][3] == "carla@example.com"
    assert by_phone["5491100000077"][3] == "x@example.com"
    assert by_phone[PHONE_UNKNOWN][2] == "Otra Persona"


def test_failed_write_raises_storage_error_and_leaves_the_cache_alone():
    ws = sheet([PHONE_PENDING, "", "", "", "", ""])
    repo = make_repo(ws)
    repo.get_user(PHONE_PENDING)

    def boom(*args, **kwargs):
        raise RuntimeError("quota")

    ws.batch_update = boom
    with pytest.raises(StorageError):
        repo.save_onboarding(PHONE_PENDING, OnboardingData(full_name="Carla"))
    assert repo.get_user(PHONE_PENDING).full_name is None
