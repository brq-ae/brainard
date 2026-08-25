"""scripts/brain-wrapper.sh -- the `fetch`/`cleanup` verbs (ADR-0012
decision 12: the hard-scoped security boundary an external-agent librarian
runs unattended, with nobody reviewing its tool calls before they execute --
see that script's own header comment and docs/librarian.md). Before this
file, the repo had NO test exercising this script at all, despite it being
the documented enforcement boundary for a destructive `cleanup` verb and a
network-fetching `fetch` verb.

Every test here drives the REAL /root/bernard-ai/scripts/brain-wrapper.sh
(resolved relative to this file, so the same path works whether pytest runs
on the host or inside the `test` compose service's own copy at
/app/scripts/brain-wrapper.sh -- both are byte-identical, COPYed verbatim by
the Dockerfile, never a rewritten or trimmed-down copy) via `subprocess`,
against a throwaway local HTTP server (`_Handler`/`_ThreadingServer` below,
a Python port of what was a standalone `wrapper_test_server.py` during
development) bound to 127.0.0.1 on an OS-assigned ephemeral port. Nothing
here ever talks to a real Brainard deployment: `BRAINARD_URL` is always
pointed at that local server, for every test, via the `wrapper_env` fixture.

The previous exploratory pass on this coverage found that backgrounding a
listener process needed `dangerouslyDisableSandbox: true` in an interactive
tool session; that constraint was specific to that ad hoc tool-use
environment, not to a normal `pytest` process. Here the server runs as an
in-process background thread (`_ThreadingServer`, daemon thread) started by
a module-scoped fixture, inside the `test` compose service's own container
-- an ordinary execution context with no special sandboxing around socket
binding. If that ever proves flaky in some environment, `wrapper_server`
below is the one place to make more robust (e.g. add a bind retry loop);
nothing else in this file should need to change.
"""

import http.server
import os
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import pytest

# Resolved relative to this file (tests/../scripts/brain-wrapper.sh) so this
# is always the real script next to wherever these tests happen to be
# running from -- /root/bernard-ai/scripts/brain-wrapper.sh on the host,
# /app/scripts/brain-wrapper.sh inside the test container -- never a copy
# written somewhere else for testability.
WRAPPER = Path(__file__).resolve().parent.parent / "scripts" / "brain-wrapper.sh"

PDF_BODY = b"%PDF-1.4\n" + b"A" * 500 + b"\n%%EOF"

BAD_ROOM_IDS = [
    "..",
    "../etc",
    "/etc/passwd",
    "room\nid",
    "room;rm -rf /",
    "room$(id)",
    "room id",
    "a/b",
]


class _Handler(http.server.BaseHTTPRequestHandler):
    """Just enough surface to exercise the wrapper's own guards (redirect
    refusal, size cap, status handling, a hostile Content-Disposition the
    wrapper must never read) -- not a Brainard API stand-in.
    """

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    def do_GET(self):
        path = self.path
        if path.startswith("/v1/rooms/") and "/attachments/" in path and path.endswith("/download"):
            token = path.split("/attachments/")[1].rsplit("/download", 1)[0]
            return self._route(token)
        self.send_response(404)
        self.end_headers()

    def _route(self, token):
        if token == "okplain":
            self._send_ok(PDF_BODY)
            return

        if token.startswith("hostile"):
            kind = token[len("hostile") :]
            hostile_values = {
                "traversal": "../../../../tmp/evil.pdf",
                "absolute": "/etc/passwd",
                "newline": 'evil.pdf"\r\nX-Injected: yes',
            }
            cd_value = hostile_values.get(kind, "evil.pdf")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(PDF_BODY)))
            try:
                self.send_header("Content-Disposition", f'attachment; filename="{cd_value}"')
            except Exception:
                # http.server may reject a raw CRLF outright -- irrelevant
                # here, since what's under test is that the wrapper never
                # reads this header at all, not that the server can send an
                # arbitrarily malformed one.
                pass
            self.end_headers()
            self.wfile.write(PDF_BODY)
            return

        if token == "oversized":
            # No Content-Length -- chunked, so curl's --max-filesize alone
            # (which needs a known size up front) cannot be what saves us;
            # only the wrapper's own piped `head -c <cap+1>` can.
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"B" * 65536
            hexlen = format(len(chunk), "x").encode()
            try:
                for _ in range(2000):  # ~128MB, far past any sane cap
                    self.wfile.write(hexlen + b"\r\n" + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if token == "redirectsamehost":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/v1/rooms/room1/attachments/okplain/download")
            self.end_headers()
            return

        if token == "redirectotherhost":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/v1/rooms/room1/attachments/okplain/download")
            self.end_headers()
            return

        if token == "slowpartial":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", "999999")
            self.end_headers()
            try:
                self.wfile.write(b"%PDF-1.4\npartial-only")
                self.wfile.flush()
            except Exception:
                pass
            time.sleep(0.3)
            try:
                self.connection.close()
            except Exception:
                pass
            return

        self.send_response(404)
        self.end_headers()

    def _send_ok(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture(scope="module")
def wrapper_server():
    # Port 0 -- let the OS pick an ephemeral free port, so this can't
    # collide with anything else (a concurrently-running instance of this
    # same suite, a leftover listener, ...). The socket is already bound by
    # the time the constructor returns, so there's no readiness race to
    # poll for (unlike the original bash version of this suite, which
    # polled a fixed port up to 50 times waiting for the server to come up).
    server = _ThreadingServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def wrapper_env(tmp_path, wrapper_server):
    """A fresh scratch dir (pytest's own per-test `tmp_path` -- no shared
    state between tests, unlike the original bash suite's single `$SCRATCH`
    it had to snapshot-diff around) and every env var the wrapper reads,
    all pointed here -- never at a real deployment.
    """
    token_file = tmp_path / "token"
    token_file.write_text("test-token-value\n")
    token_file.chmod(0o600)
    attachments_dir = tmp_path / "attachments"

    env = dict(os.environ)
    env.update(
        {
            "BRAINARD_URL": wrapper_server,
            "BRAINARD_TOKEN_FILE": str(token_file),
            "BRAINARD_ATTACHMENTS_DIR": str(attachments_dir),
            "BRAINARD_FETCH_MAX_BYTES": "100000",  # 100KB -- low, so the oversized case runs fast
        }
    )
    return env


def _run(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, timeout=30, env=env)


def _room_dir(env: dict, room: str) -> Path:
    return Path(env["BRAINARD_ATTACHMENTS_DIR"]) / room


# --- 1. room id validation: '..', '/', absolute path, newline, shell
# metacharacters -- refused before forming any path or URL segment ---


@pytest.mark.parametrize("bad_id", BAD_ROOM_IDS)
def test_fetch_refuses_bad_room_id(bad_id, wrapper_env):
    result = _run("fetch", bad_id, "attach1", env=wrapper_env)
    assert result.returncode != 0
    assert "room id" in result.stderr


@pytest.mark.parametrize("bad_id", BAD_ROOM_IDS)
def test_cleanup_refuses_bad_room_id(bad_id, wrapper_env):
    result = _run("cleanup", bad_id, env=wrapper_env)
    assert result.returncode != 0
    assert "room id" in result.stderr


def test_room_id_with_embedded_null_byte_cannot_even_be_constructed_via_python(wrapper_env):
    """The original bash-driven version of this suite could pass a room id
    with a literal embedded NUL byte, relying on execve()/argv being
    NUL-terminated C strings: the OS silently truncates "room\\x00id" to
    "room" before brain-wrapper.sh ever runs, and "room" alone is just an
    ordinary valid bare id (its fetch would 404 against an unknown
    attachment id, already covered in spirit by the bad-room-id cases
    above and the plain fetch/cleanup success cases below). Python's
    `subprocess` module can't reproduce that exact call shape -- it
    validates every argument for an embedded NUL and refuses to exec at
    all, for both `str` and `bytes` arguments alike -- a stricter, earlier
    guarantee than the OS-level truncation this script's own design
    already tolerates. Documented here (and proven) rather than silently
    dropped.
    """
    with pytest.raises(ValueError, match="null"):
        subprocess.run([str(WRAPPER), "fetch", "room\x00id", "attach1"], capture_output=True, env=wrapper_env)


# --- 2. a valid fetch writes only inside the room dir ---


def test_fetch_writes_file_at_expected_path_inside_room_dir(wrapper_env):
    room = "room1valid"
    result = _run("fetch", room, "okplain", "myfile.pdf", env=wrapper_env)
    expected = _room_dir(wrapper_env, room) / "myfile.pdf"

    assert result.returncode == 0, result.stderr
    assert expected.is_file()
    assert str(expected) in result.stdout
    assert expected.stat().st_size > 0

    base = Path(wrapper_env["BRAINARD_ATTACHMENTS_DIR"])
    assert [p.name for p in base.iterdir()] == [room]  # no stray entries at the base level


# --- 3. hostile Content-Disposition is never read, so it can never place a
# file outside the room dir ---


@pytest.mark.parametrize("kind", ["traversal", "absolute", "newline"])
def test_fetch_hostile_content_disposition_never_escapes_room_dir(kind, wrapper_env, tmp_path):
    room = f"hostileroom{kind}"
    result = _run("fetch", room, f"hostile{kind}", env=wrapper_env)
    assert result.returncode == 0, result.stderr

    room_dir = _room_dir(wrapper_env, room)
    token_file = Path(wrapper_env["BRAINARD_TOKEN_FILE"])
    all_files = [p for p in tmp_path.rglob("*") if p.is_file() and p != token_file]
    escaped = [p for p in all_files if room_dir not in p.parents]
    assert not escaped, escaped


# --- hostile filename ARGUMENT: worst case, an agent naively forwards a
# hostile string (e.g. something it saw in a header elsewhere) as the
# display filename directly -- must still be sanitized, not just the
# server's own header ---


def test_fetch_hostile_filename_argument_traversal_sanitized(wrapper_env):
    room = "hostilearg"
    _run("fetch", room, "okplain", "../../../../tmp/evil.pdf", env=wrapper_env)
    room_dir = _room_dir(wrapper_env, room)
    found = list(room_dir.glob("*"))
    assert len(found) == 1
    assert not Path("/tmp/evil.pdf").exists()


def test_fetch_hostile_filename_argument_absolute_path_sanitized(wrapper_env):
    room = "hostileabs"
    _run("fetch", room, "okplain", "/etc/passwd", env=wrapper_env)
    room_dir = _room_dir(wrapper_env, room)
    found = list(room_dir.glob("*"))
    assert len(found) == 1
    assert found[0].name != "passwd"
    assert found[0].parent == room_dir


# --- 4. oversized response aborts, nothing left ---


def test_fetch_oversized_response_aborts_with_nothing_left(wrapper_env):
    room = "oversizedroom"
    result = _run("fetch", room, "oversized", env=wrapper_env)
    assert result.returncode != 0
    room_dir = _room_dir(wrapper_env, room)
    leftover = list(room_dir.glob("*")) if room_dir.exists() else []
    assert leftover == []


# --- 5. redirects refused, same host and cross host alike ---


def test_fetch_same_host_redirect_refused(wrapper_env):
    room = "redirroom1"
    result = _run("fetch", room, "redirectsamehost", env=wrapper_env)
    assert result.returncode != 0
    room_dir = _room_dir(wrapper_env, room)
    assert not (room_dir.exists() and any(room_dir.glob("*")))


def test_fetch_cross_host_redirect_refused(wrapper_env):
    room = "redirroom2"
    result = _run("fetch", room, "redirectotherhost", env=wrapper_env)
    assert result.returncode != 0
    room_dir = _room_dir(wrapper_env, room)
    assert not (room_dir.exists() and any(room_dir.glob("*")))


# --- 6. a died-mid-transfer response leaves no partial file ---


def test_fetch_died_mid_transfer_leaves_no_partial_file(wrapper_env):
    room = "partialroom"
    result = _run("fetch", room, "slowpartial", env=wrapper_env)
    assert result.returncode != 0
    room_dir = _room_dir(wrapper_env, room)
    assert not (room_dir.exists() and any(room_dir.glob("*")))


# --- 7. cleanup removes only its own room's contents, leaves siblings and
# the base dir intact ---


def test_cleanup_removes_only_its_own_room(wrapper_env):
    room_a, room_b = "cleanupA", "cleanupB"
    _run("fetch", room_a, "okplain", "a.pdf", env=wrapper_env)
    _run("fetch", room_b, "okplain", "b.pdf", env=wrapper_env)
    base = Path(wrapper_env["BRAINARD_ATTACHMENTS_DIR"])
    assert (base / room_a / "a.pdf").is_file()
    assert (base / room_b / "b.pdf").is_file()

    result = _run("cleanup", room_a, env=wrapper_env)

    assert result.returncode == 0, result.stderr
    assert not (base / room_a / "a.pdf").exists()
    assert (base / room_a).is_dir()  # the slot itself survives -- only its contents are removed
    assert (base / room_b / "b.pdf").is_file()
    assert base.is_dir()


# --- 8. cleanup refuses a room slot that is a symlink pointing outside the
# base dir ---


def test_cleanup_refuses_symlinked_room_slot(wrapper_env, tmp_path):
    base = Path(wrapper_env["BRAINARD_ATTACHMENTS_DIR"])
    base.mkdir(parents=True, exist_ok=True)
    outside_target = tmp_path / "outside_target"
    outside_target.mkdir()
    survivor = outside_target / "should_survive.txt"
    survivor.write_text("keep me")

    room = "escaperoom"
    (base / room).symlink_to(outside_target, target_is_directory=True)

    result = _run("cleanup", room, env=wrapper_env)

    assert result.returncode != 0
    assert survivor.is_file()
    assert survivor.read_text() == "keep me"


# --- 9. cleanup on a room with no local directory is a harmless no-op ---


def test_cleanup_on_missing_room_is_a_no_op(wrapper_env):
    result = _run("cleanup", "neverexistedroom", env=wrapper_env)
    assert result.returncode == 0
    assert "nothing to clean up" in result.stdout


# --- 10. cleanup's realpath-confinement check (scripts/brain-wrapper.sh's
# `cleanup` case, the `RESOLVED="$(realpath -e -- "$ROOM_DIR")"` /
# `case "$RESOLVED" in "$BASE_RESOLVED"/*) ;; ...` block) is present and
# correctly formed -- a STRUCTURAL test, not a behavioral one, and
# deliberately so. Explanation for why a behavioral test isn't possible:
#
# Every path this verb ever builds is `$BRAINARD_ATTACHMENTS_DIR/$ROOM_ID`
# where `$ROOM_ID` has already passed `_valid_bare_id` (`^[A-Za-z0-9]{1,64}$`
# -- no `/`, no `.`, no whitespace, no `..`), so `$ROOM_DIR` is always
# exactly the base dir plus one single, non-traversing path component: there
# is no room id that can make that component resolve outside the base by
# itself. The one way a single trailing component *could* still resolve
# outside the base -- the room slot being a symlink to somewhere else -- is
# already caught by the earlier `[[ -L "$ROOM_DIR" ]]` check (line ~335,
# `test_cleanup_refuses_symlinked_room_slot` above) and exits before this
# confinement check ever runs. So there is no reachable combination of a
# CLI-suppliable room id plus on-disk state that passes both the id-format
# gate and the symlink gate while still failing the confinement check --
# confirmed empirically against scratch copies of the script (never the
# shipped one) during review: deleting the confinement block does not fail
# any existing behavioral test, while deleting `_valid_bare_id`'s enforcement
# does (`test_cleanup_refuses_bad_room_id`). It is real defence-in-depth --
# it would matter the moment either upstream guard's invariant changed (e.g.
# `_valid_bare_id` ever allowed `/`) -- so this test exists to make removing
# it fail loudly, just via a structural assertion instead of a contrived,
# unreachable-in-practice input.


def test_cleanup_realpath_confinement_guard_is_present():
    script = WRAPPER.read_text()
    cleanup_block = script.split("\n  cleanup)", 1)[1]
    cleanup_block = cleanup_block.split("\nesac", 1)[0]

    # The symlinked-room-slot check runs first and must still be there.
    assert '[[ -L "$ROOM_DIR" ]]' in cleanup_block

    # The confinement check itself: resolve the room dir for real...
    assert 'RESOLVED="$(realpath -e -- "$ROOM_DIR")"' in cleanup_block
    # ...compare it against the resolved base dir...
    assert 'BASE_RESOLVED="$(realpath -e -- "$BRAINARD_ATTACHMENTS_DIR")"' in cleanup_block
    # ...refuse anything that isn't strictly a child of the base dir...
    assert '"$BASE_RESOLVED"/*' in cleanup_block
    # ...and refuse the base dir itself (an exact match is not "inside" it).
    assert 'if [[ "$RESOLVED" == "$BASE_RESOLVED" ]]' in cleanup_block

    # Ordering: the symlink check must appear before the confinement check,
    # matching the reasoning above (symlink gate closes off the only way the
    # confinement check could otherwise be reached).
    assert cleanup_block.index('[[ -L "$ROOM_DIR" ]]') < cleanup_block.index(
        'RESOLVED="$(realpath -e -- "$ROOM_DIR")"'
    )

    # And only THEN does anything destructive happen -- the confinement
    # check must precede the actual delete.
    assert cleanup_block.index('"$BASE_RESOLVED"/*') < cleanup_block.index("rm -rf")
