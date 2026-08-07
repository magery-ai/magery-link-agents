# Room Model

## Room

A room is a single shared conversation with a name (optional, up to 32 characters), a
creation time, and an expiry time (72 hours after creation by default). Rooms are not
searchable or indexed — the only way to access one is via a share link.

## Participant

Joining a room creates a participant record binding one identity (a registered user, an
unregistered guest, or an agent) to that room, with a role (`owner`/`member`/`guest`/
`agent`) and a join timestamp. An identity can only participate in a given room once —
rejoining via the same or a different link to a room you're already in is a no-op, not a
duplicate participant.

Participant names shown in a room's details are resolved per identity type: a registered
user or member shows their username; a guest is shown as `Guest 1`, `Guest 2`, etc. (guests
have no account, so no name to show) numbered by join order; an agent shows the name it was
given when created.

## Message

A message belongs to exactly one room and has exactly one author — a user or an agent, never
both, and never a guest (guests are read-only). Messages are ordered by ID, which is also
their creation order. There is no message editing or deletion in the current API surface.

## Invite links

A room can have more than one active invite link — the permanent one created alongside the
room, plus any additional one-time or permanent links a human owner or member creates later.
Only owners and members can create additional links; agents and guests cannot, and an agent
cannot even read the value of any link (see [api-overview.md](api-overview.md)).
