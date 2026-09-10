"""Room seat assignment/binding (ADR-0018) -- the one shared function both
`app/rooms.py`'s `post_message` and `app/attachments.py`'s
`add_room_attachment` call, under the room's row lock, before accepting a
write from an agent.

Lives in its own module, below both callers, for the same circularity
reason `app/attachments.py`'s `_sender_is_member` already documents:
`app/rooms.py` imports `app/attachments.py` (for `delete_room`'s blob-reclaim
call), so the reverse import would be circular. A seat bind-or-check is a
conditional WRITE performed under a lock -- unlike a read-only membership
check, two independently-maintained copies of that would be exactly the
kind of security-sensitive logic that drifts, so it is factored out here
once instead.

ADR-0018 decision 3: a `RoomMember` seat binds to exactly one `Machine`, via
either of two paths that both just set `RoomMember.bound_machine_id`:
optional assignment at room-creation time (`app/rooms.py`'s `create_room`),
or claim-on-first-write (this module, the first time a machine token posts
or attaches as that member). `check_and_bind_seat` below is what enforces
both paths identically: if the column is already set, only that machine may
proceed; if it's NULL, this call is what sets it.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.errors import ApiError
from app.models import Room, RoomMember

# ADR-0018 decision 9: directive, enveloped refusal -- same register as
# app/rooms.py's `_ROOM_NOT_OPENED_DIRECTIVE`/`_CLOSE_AS_AGREED_DIRECTIVE`:
# names the seat, states plainly this is not a retryable error, and tells
# the agent exactly what to do (stop; this is not your room; tell the
# owner) rather than leaving it to infer that from a bare 403. Deliberately
# does NOT name the machine actually bound to the seat -- the caller has no
# legitimate need to know which other credential holds it, only that it
# isn't theirs.
_SEAT_BOUND_TO_OTHER_MACHINE_DIRECTIVE = (
    "Stop: do not post, do not attach, and do not retry -- this is not a transient error. This is not your "
    "room. If you believe this is wrong (a restarted agent, a re-minted machine token, or you were handed the "
    "wrong seat), do not act on it yourself: tell your operator. Only the room's owner can release or "
    "reassign this seat."
)


def _seat_bound_to_other_machine_detail(room: Room, sender: str) -> str:
    return (
        f"The seat '{sender}' in room '{room.name}' is bound to a different machine token than the one you "
        f"authenticated with. {_SEAT_BOUND_TO_OTHER_MACHINE_DIRECTIVE}"
    )


async def check_and_bind_seat(db: AsyncSession, room: Room, sender: str, principal: Principal) -> None:
    """Must be called under the room's row lock (`post_message`'s and
    `add_room_attachment`'s own `SELECT ... FOR UPDATE` acquisition), after
    the caller has already confirmed `sender` is a real member (or the
    literal 'owner') of this room -- this function does not re-derive that;
    it treats "no such member" as an unrelated concern already handled by
    the caller's own membership check and no-ops rather than raising a
    second, differently-worded error for the same condition.

    ADR-0018 decision 6: no-ops whenever `principal.kind != "machine"` --
    i.e. whenever the request is authenticated with the owner token,
    regardless of which `sender` string the owner's own request happens to
    post as. Keyed on the AUTHENTICATED principal, never on the `sender`
    string, for the identical reason `app/rooms.py`'s `sender == "owner"`
    impersonation check is keyed on `principal.kind` and not on the literal
    text -- a constraint on agents, never on the owner's control of their
    own room.

    For a genuine machine principal: looks up the `(room.id, sender)`
    `RoomMember` row directly (a plain query, not `app.rooms.get_members`,
    same circularity reasoning as `app/attachments.py`'s `_sender_is_member`
    -- see this module's docstring). Three outcomes:
      - No such member: no-op (the caller's own pre-lock membership check
        already rejects this before this function is ever reached; this is
        defense in depth, not the primary guard).
      - `bound_machine_id` is NULL: this is either the seat's first-ever
        write (claim-on-first-write, decision 3 second path) -- set it to
        `principal.machine.id` -- and the call proceeds.
      - `bound_machine_id` is already set: if it matches
        `principal.machine.id`, no-op (the ordinary case of the seat's
        bound/assigned machine posting again). If it does NOT match, raise
        `ApiError(403, "seat_bound_to_other_machine", ...)` -- the seat was
        either assigned to a different machine at create time, or already
        claimed by a different machine's earlier post/attach.
    """
    if principal.kind != "machine":
        return

    member = await db.scalar(select(RoomMember).where(RoomMember.room_id == room.id, RoomMember.agent_name == sender))
    if member is None:
        return

    machine_id = principal.machine.id
    if member.bound_machine_id is None:
        member.bound_machine_id = machine_id
        return

    if member.bound_machine_id != machine_id:
        raise ApiError(403, "seat_bound_to_other_machine", _seat_bound_to_other_machine_detail(room, sender))
