"""Table reservations.

**Slot model** (no slot policy existed before, so this is the smallest one
that is deterministic and database-enforceable): the day is split into fixed,
non-overlapping two-hour slots starting at ``SLOT_STARTS``. A reservation
books exactly one table for exactly one slot, so two bookings conflict iff
they share (table, date, slot) -- which ``uq_reservations_table_slot`` makes
impossible to commit twice, even under concurrency.

**Table status vs reservations.** ``RestaurantTable.status`` is *current*
floor state (who is sitting there now). Future availability is answered only
from this module's slot bookings; the one current status that also blocks
future bookings is OUT_OF_SERVICE. A table being OCCUPIED or CLEANING right
now says nothing about 19:00 next Friday, and a booking never changes the
table's current status.

**Ownership.** Every write takes the owning ``User`` from the caller, which
takes it from the session -- no function here accepts a user id from a form.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models.reservation import Reservation, ReservationStatus
from app.models.restaurant_table import RestaurantTable, TableStatus
from app.models.user import User
from app.services.errors import ValidationError
from app.utils.clock import db_now

SLOT_STARTS = tuple(time(hour) for hour in (11, 13, 15, 17, 19, 21))
MAX_DAYS_AHEAD = 60

R = ReservationStatus
ALLOWED_RESERVATION_TRANSITIONS: dict[ReservationStatus, frozenset[ReservationStatus]] = {
    R.CONFIRMED: frozenset({R.COMPLETED, R.NO_SHOW, R.CANCELLED}),
    R.COMPLETED: frozenset(),
    R.NO_SHOW: frozenset(),
    R.CANCELLED: frozenset(),
}


class ReservationError(ValidationError):
    pass


def _fail(field: str, message: str):
    raise ReservationError({field: [message]})


def parse_slot(raw_date, raw_time, raw_guests) -> tuple[date, time, int]:
    """Validate the requested slot and party size. Accepts strings (query
    string / form) or already-typed values."""
    try:
        day = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date or ""))
    except ValueError:
        _fail("reservation_date", "Choose a valid date.")
    try:
        slot = raw_time if isinstance(raw_time, time) else time.fromisoformat(str(raw_time or ""))
    except ValueError:
        _fail("reservation_time", "Choose a valid time.")
    if slot not in SLOT_STARTS:
        _fail("reservation_time", "Choose one of the listed booking times.")
    try:
        guests = int(raw_guests)
    except (TypeError, ValueError):
        _fail("guest_count", "Enter the number of guests.")
    if guests < 1:
        _fail("guest_count", "At least one guest is required.")

    now = db_now()
    if datetime.combine(day, slot) <= now:
        _fail("reservation_date", "That time has already passed.")
    if day > now.date() + timedelta(days=MAX_DAYS_AHEAD):
        _fail("reservation_date", f"Bookings open at most {MAX_DAYS_AHEAD} days ahead.")
    return day, slot, guests


def _booked_table_ids(day: date, slot: time) -> set[int]:
    return set(db.session.scalars(
        sa.select(Reservation.table_id).where(
            Reservation.reservation_date == day, Reservation.reservation_time == slot,
            Reservation.holds_slot.is_(True),
        )
    ))


def available_tables(day: date, slot: time, guests: int) -> list[RestaurantTable]:
    """Tables that can take this party in this slot: in service, big enough,
    and not already booked for the slot. Current floor status is ignored."""
    booked = _booked_table_ids(day, slot)
    return [
        t for t in db.session.query(RestaurantTable).order_by(RestaurantTable.capacity, RestaurantTable.name)
        if t.status != TableStatus.OUT_OF_SERVICE and t.capacity >= guests and t.id not in booked
    ]


def create_reservation(owner: User, raw_table_id, raw_date, raw_time, raw_guests) -> Reservation:
    day, slot, guests = parse_slot(raw_date, raw_time, raw_guests)
    try:
        table = db.session.get(RestaurantTable, int(raw_table_id))
    except (TypeError, ValueError):
        table = None
    if table is None:
        _fail("table_id", "Choose a table.")
    if table.status == TableStatus.OUT_OF_SERVICE:
        _fail("table_id", f"{table.name} is out of service.")
    if guests > table.capacity:
        _fail("guest_count", f"{table.name} seats at most {table.capacity}.")

    reservation = Reservation(
        user_id=owner.id, table_id=table.id, reservation_date=day, reservation_time=slot,
        guest_count=guests, status=ReservationStatus.CONFIRMED, holds_slot=True,
    )
    db.session.add(reservation)
    try:
        # No pre-check for a clash: the unique index is the check, and it is
        # the only one that holds when two customers submit at once.
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _fail("table_id", f"{table.name} has just been booked for that time. Please choose another table or time.")
    return reservation


def _apply(reservation: Reservation, from_status: ReservationStatus, new_status: ReservationStatus,
           owner_id: int | None = None) -> bool:
    """Conditional UPDATE -- same concurrency pattern as order transitions."""
    stmt = (
        sa.update(Reservation)
        .where(Reservation.id == reservation.id, Reservation.status == from_status)
        .values(status=new_status, holds_slot=None if new_status == ReservationStatus.CANCELLED else True)
        .execution_options(synchronize_session=False)
    )
    if owner_id is not None:
        stmt = stmt.where(Reservation.user_id == owner_id)
    try:
        won = db.session.execute(stmt).rowcount == 1
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        _fail("status", "Could not update the reservation. Please try again.")
    db.session.refresh(reservation)
    return won


def customer_can_cancel(reservation: Reservation) -> bool:
    return (reservation.status == ReservationStatus.CONFIRMED
            and datetime.combine(reservation.reservation_date, reservation.reservation_time) > db_now())


def cancel_reservation_as_customer(reservation: Reservation, owner: User) -> ReservationStatus:
    previous = reservation.status
    if reservation.user_id != owner.id:
        _fail("status", "Reservation not found.")
    if not customer_can_cancel(reservation) or not _apply(
        reservation, ReservationStatus.CONFIRMED, ReservationStatus.CANCELLED, owner_id=owner.id
    ):
        _fail("status", f"This reservation is {reservation.status.value.replace('_', ' ')} "
                        "and can no longer be cancelled online.")
    return previous


def update_reservation_status(reservation: Reservation, raw_status) -> ReservationStatus:
    try:
        new_status = ReservationStatus(raw_status)
    except ValueError:
        _fail("status", "Unknown reservation status.")
    previous = reservation.status
    if new_status not in ALLOWED_RESERVATION_TRANSITIONS[previous] or not _apply(reservation, previous, new_status):
        _fail("status", f"Cannot change a reservation from {reservation.status.value} to {new_status.value}.")
    return previous


def get_reservation_for_user(reservation_id: int, user_id: int) -> Reservation | None:
    """Ownership in the query, as with orders: someone else's id and a
    nonexistent id are indistinguishable (both None -> 404)."""
    return db.session.query(Reservation).filter_by(id=reservation_id, user_id=user_id).first()


def list_reservations_for_user(user_id: int) -> list[Reservation]:
    return (
        db.session.query(Reservation).options(selectinload(Reservation.table))
        .filter_by(user_id=user_id)
        .order_by(Reservation.reservation_date.desc(), Reservation.reservation_time.desc())
        .all()
    )


def list_reservations_for_staff(since: date) -> list[Reservation]:
    return (
        db.session.query(Reservation)
        .options(selectinload(Reservation.table), selectinload(Reservation.user))
        .filter(Reservation.reservation_date >= since)
        .order_by(Reservation.reservation_date, Reservation.reservation_time, Reservation.id)
        .all()
    )
