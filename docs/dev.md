# Developer notes

## Running the stack

```
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD, then mirror it into DATABASE_URL and
# TEST_DATABASE_URL (all three must agree on user/password/host/port)

docker compose up -d --build
docker compose logs api   # the owner token is printed here once, on first boot only
```

`GET /healthz` is unauthenticated and reports database reachability. The API is published on `API_PORT` (default `8300`).

## Running tests

Tests run against a real Postgres database: the same `db` service, but a separate `brain_test` database (created automatically by the test suite on first run), via a profile-gated `test` compose service that is never started by plain `docker compose up`.

```
docker compose up -d db
docker compose --profile test build test
docker compose --profile test run --rm test
```

## Tearing down

```
docker compose down          # stop containers, keep named volumes (data persists)
docker compose down -v       # also remove volumes (destroys all data)
```
