# Northwind Fleet API v2 - reference

The Fleet API lets business customers manage fleets of Northwind robots: list robots, schedule
cleaning missions and receive events. Base URL: https://api.northwind.example/v2

## Authentication

Every request needs a bearer token in the Authorization header. Tokens are created in the
customer console and expire after 90 days. Tokens are scoped either read-only or read-write;
scheduling missions requires a read-write token.

## Rate limits

Each organisation may send up to 600 requests per minute. Above the limit the API answers HTTP
429 with the error code FLT-429 and a Retry-After header giving the number of seconds to wait.
Bulk endpoints count as one request per item.

## Pagination

List endpoints return at most 100 items per page. The response contains a next_cursor field;
pass it as the cursor query parameter to fetch the next page. A null next_cursor means the
last page was reached. Cursors expire after 10 minutes.

## Missions

POST /v2/robots/{robot_id}/missions schedules a cleaning mission. The body must contain a
map_id and a list of room_ids; an optional start_at field schedules the mission in the future.
A robot can hold at most 20 scheduled missions.

## Error codes

- FLT-400: malformed request body.
- FLT-401: missing or expired token.
- FLT-403: the token is read-only.
- FLT-404: robot not found in your organisation.
- FLT-4012: invalid map_id, the map does not belong to this robot.
- FLT-409: the robot already runs a mission.
- FLT-429: rate limit exceeded.

## Webhooks

Webhooks notify your server of mission and robot events. Each delivery is signed with an
HMAC-SHA256 signature in the X-Northwind-Signature header, computed with your webhook secret.
If your endpoint does not answer with a 2xx status within 10 seconds, the delivery is retried
up to 8 times with exponential backoff over about 24 hours, after which the event is dropped.
