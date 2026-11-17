# Release notes

## Firmware 3.3.0 (R7)

- Carpet boost: the R7 raises suction automatically on carpets.
- Pet mode: avoids pet bowls detected by the front camera.
- Battery: Eco mode runtime improved by 10 minutes.

## Firmware 3.2.1 (R5 and R7)

- Fixed false E-204 errors on dark floors.
- Fixed the robot not returning to its dock after a map update.

## Firmware 3.2.0 (R7)

- No-go zones: draw areas in the app that the robot must avoid.
- Room naming: rooms can be renamed and used in voice commands.

## Fleet API

- The Fleet API v1 is deprecated and will be shut down on 31 March 2027. Migrate to v2 before
  that date; v1 tokens do not work with v2.
- v2.4 added the start_at field for scheduling missions in the future.
- v2.3 added webhook signatures with HMAC-SHA256.
