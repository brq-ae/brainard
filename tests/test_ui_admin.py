"""UI admin area: machine mint (token shown once) + revoke, proposal
approve/reject (phase 6). Exercises the same shared logic
(app/machines.py, app/proposals.py) as the API endpoints -- see
tests/test_machines.py and tests/test_proposals.py for the API-side
equivalents.
"""

import html
import re

from sqlalchemy import select
from ulid import ULID

import app.librarian_engine as librarian_engine_module
from app.models import Machine, OwnerToken
from app.security import generate_machine_token, generate_owner_token, hash_token


async def _login(client, db_session) -> str:
    token = generate_owner_token()
    db_session.add(OwnerToken(token_hash=hash_token(token)))
    await db_session.commit()
    await client.post("/ui/login", data={"token": token})
    return token


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf_token hidden field not found in page"
    return m.group(1)


async def _machine_headers(db_session) -> dict:
    token = generate_machine_token()
    db_session.add(Machine(id=str(ULID()), name="m1", token_hash=hash_token(token), status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {token}"}


# --- machines ---


async def test_ui_mint_machine_shows_token_once(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/admin/machines", data={"name": "ui-minted-machine", "csrf_token": csrf})
    assert resp.status_code == 201
    assert "brn_" in resp.text
    assert "ui-minted-machine" in resp.text

    token_match = re.search(r"brn_[A-Za-z0-9_-]+", resp.text)
    assert token_match, "minted token not found in response"
    minted_token = token_match.group(0)

    # the minted token actually authenticates against the API
    api_resp = await client.get("/v1/machines", headers={"Authorization": f"Bearer {minted_token}"})
    # a machine token is not owner-scoped -- 403, but that proves it's a
    # real, valid, *authenticating* token (401 would mean it failed to parse)
    assert api_resp.status_code == 403
    assert api_resp.json()["error"]["code"] == "owner_token_required"

    # the token is never shown again on a plain re-list of the page
    listing = await client.get("/ui/admin/machines")
    assert minted_token not in listing.text
    assert "ui-minted-machine" in listing.text


async def test_ui_mint_machine_shows_onboarding_prompt(client, db_session):
    """Phase 7 addition (docs/onboarding.md), superseded by the "machine
    roles + prebuilt onboarding prompt" feature: the show-once mint page
    displays the FULL generated onboarding prompt with the hub URL and the
    fresh token already filled in.
    """
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/admin/machines", data={"name": "paste-line-machine", "csrf_token": csrf})
    assert resp.status_code == 201

    token_match = re.search(r"brn_[A-Za-z0-9_-]+", resp.text)
    assert token_match, "minted token not found in response"
    minted_token = token_match.group(0)

    # Jinja2 autoescape turns '<'/'>'/"'" into HTML entities -- unescape
    # before asserting on the literal prompt text.
    body = html.unescape(resp.text)
    assert "I run a private knowledge hub for my projects" in body
    assert "/v1/bootstrap?project=<PROJECT>" in body
    assert f"Bearer {minted_token}" in body
    # Trust anchored in the owner's own message, judgment preserved -- never
    # an obedience-demand (docs/onboarding.md "Why it's worded this way":
    # "follow the returned instructions exactly" was refused by a
    # well-defended AI elsewhere as prompt-injection-shaped).
    assert "apply it with your normal judgment" in body
    # Patch 2026-08-07 (docs/onboarding.md "Naming: the owner assigns
    # project slugs"): the mint page reinforces, right under the prompt,
    # that <PROJECT> is the owner's call, never the AI's guess.
    assert "Replace <PROJECT> with the project slug YOU choose" in body
    assert "don't let the AI pick" in body
    assert "it never overrides your safety rules" in body
    # solo (the default role) contributes no role paragraph
    assert "You are the Commander" not in body
    assert "You are the Builder" not in body

    # the REAL token is never shown again on a plain re-list of the page --
    # every machine (including this one) does now show a regenerated
    # onboarding prompt in its own expander, but always with the
    # placeholder standing in for the token, never the real value.
    listing = await client.get("/ui/admin/machines")
    assert minted_token not in listing.text
    assert "id=\"new-prompt\"" not in listing.text
    assert "id=\"new-token\"" not in listing.text
    listing_body = html.unescape(listing.text)
    assert "&lt;token&gt;" in listing_body or "<token>" in listing_body


async def test_ui_mint_with_commander_role_renders_full_prompt(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/admin/machines",
        data={
            "name": "commander-box",
            "role": "commander",
            "default_project": "my-project",
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 201
    body = html.unescape(resp.text)
    assert "You are the Commander for this project." in body
    assert "You own ALL writes to the hub" in body
    assert "You are the Builder" not in body
    # a given default project fills the prompt's project slot directly
    assert "/v1/bootstrap?project=my-project" in body


async def test_ui_mint_with_builder_role_renders_full_prompt(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/admin/machines", data={"name": "builder-box", "role": "builder", "csrf_token": csrf}
    )
    assert resp.status_code == 201
    body = html.unescape(resp.text)
    assert "You are the Builder for this project." in body
    assert "do NOT deposit anything" in body
    assert "You are the Commander" not in body


async def test_ui_mint_with_solo_role_has_no_role_text(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/admin/machines", data={"name": "solo-box", "csrf_token": csrf})
    assert resp.status_code == 201
    body = html.unescape(resp.text)
    assert "You are the Commander" not in body
    assert "You are the Builder" not in body


async def test_ui_mint_rejects_bad_role(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/admin/machines", data={"name": "bad-role-box", "role": "overlord", "csrf_token": csrf}
    )
    assert resp.status_code == 422


async def test_ui_mint_xss_probe_on_name_and_project_fields(client, db_session):
    """Machine name and default_project both flow into the rendered
    onboarding prompt (agent_name / project slot) -- confirm Jinja2
    autoescape holds and a <script> payload never reaches the response
    unescaped (contracts-v1.md/app/templates_env.py: autoescape forced on).
    """
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    evil_name = "<script>alert('name')</script>"
    evil_project = "<script>alert('project')</script>"
    resp = await client.post(
        "/ui/admin/machines",
        data={"name": evil_name, "default_project": evil_project, "csrf_token": csrf},
    )
    assert resp.status_code == 201
    assert "<script>alert('name')</script>" not in resp.text
    assert "<script>alert('project')</script>" not in resp.text
    # the escaped forms are present -- proves the values made it into the
    # response (in the prompt / list row), just safely encoded
    assert "&lt;script&gt;" in resp.text

    listing = await client.get("/ui/admin/machines")
    assert "<script>alert('name')</script>" not in listing.text
    assert "<script>alert('project')</script>" not in listing.text


async def test_ui_patch_role_via_csrf_form_works(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    mint_resp = await client.post("/ui/admin/machines", data={"name": "to-be-updated", "csrf_token": csrf})
    assert mint_resp.status_code == 201
    machine = (await db_session.execute(Machine.__table__.select().where(Machine.name == "to-be-updated"))).first()
    machine_id = machine.id
    assert machine.role == "solo"

    resp = await client.post(
        f"/ui/admin/machines/{machine_id}/update",
        data={
            "name": "to-be-updated",
            "role": "builder",
            "default_project": "updated-project",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/admin/machines"

    from app.models import Machine as MachineModel

    refreshed = await db_session.get(MachineModel, machine_id)
    assert refreshed.role == "builder"
    assert refreshed.default_project == "updated-project"

    listing = await client.get("/ui/admin/machines")
    assert "You are the Builder for this project." in html.unescape(listing.text)


async def test_ui_patch_role_requires_csrf(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    mint_resp = await client.post("/ui/admin/machines", data={"name": "csrf-protected", "csrf_token": csrf})
    machine = (await db_session.execute(Machine.__table__.select().where(Machine.name == "csrf-protected"))).first()

    resp = await client.post(
        f"/ui/admin/machines/{machine.id}/update", data={"name": "csrf-protected", "role": "builder"}
    )
    assert resp.status_code == 403


async def test_ui_patch_unknown_machine_404(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post(
        "/ui/admin/machines/01UNKNOWNIDXXXXXXXXXXXXXX/update",
        data={"name": "whatever", "role": "builder", "csrf_token": csrf},
    )
    assert resp.status_code == 404


# --- rename (feature: change a machine's name via the admin update form) ---


async def test_ui_rename_machine_via_update_form_persists(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    mint_resp = await client.post("/ui/admin/machines", data={"name": "rename-me", "csrf_token": csrf})
    assert mint_resp.status_code == 201
    machine = (await db_session.execute(Machine.__table__.select().where(Machine.name == "rename-me"))).first()
    machine_id = machine.id

    resp = await client.post(
        f"/ui/admin/machines/{machine_id}/update",
        data={
            "name": "renamed-machine",
            "role": machine.role,
            "default_project": "",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/admin/machines"

    from app.models import Machine as MachineModel

    refreshed = await db_session.get(MachineModel, machine_id)
    assert refreshed.name == "renamed-machine"

    listing = await client.get("/ui/admin/machines")
    assert "renamed-machine" in listing.text
    assert "rename-me" not in listing.text


async def test_ui_rename_machine_blank_name_rejected(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    mint_resp = await client.post("/ui/admin/machines", data={"name": "blank-rename-box", "csrf_token": csrf})
    assert mint_resp.status_code == 201
    machine = (
        await db_session.execute(Machine.__table__.select().where(Machine.name == "blank-rename-box"))
    ).first()

    resp = await client.post(
        f"/ui/admin/machines/{machine.id}/update",
        data={"name": "   ", "role": "solo", "default_project": "", "csrf_token": csrf},
    )
    assert resp.status_code == 422

    from app.models import Machine as MachineModel

    refreshed = await db_session.get(MachineModel, machine.id)
    assert refreshed.name == "blank-rename-box"  # unchanged


async def test_ui_rename_machine_xss_probe_escaped_in_list(client, db_session):
    """A machine renamed to a value containing `<script>` must render
    escaped in the admin list -- same autoescape guarantee as the mint-time
    XSS probe above (test_ui_mint_xss_probe_on_name_and_project_fields),
    now exercised through the rename path.
    """
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    mint_resp = await client.post("/ui/admin/machines", data={"name": "xss-rename-box", "csrf_token": csrf})
    assert mint_resp.status_code == 201
    machine = (
        await db_session.execute(Machine.__table__.select().where(Machine.name == "xss-rename-box"))
    ).first()

    evil_name = "<script>alert('renamed')</script>"
    resp = await client.post(
        f"/ui/admin/machines/{machine.id}/update",
        data={"name": evil_name, "role": "solo", "default_project": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    listing = await client.get("/ui/admin/machines")
    assert "<script>alert('renamed')</script>" not in listing.text
    assert "&lt;script&gt;alert(&#39;renamed&#39;)&lt;/script&gt;" in listing.text or "&lt;script&gt;" in listing.text

    from app.models import Machine as MachineModel

    refreshed = await db_session.get(MachineModel, machine.id)
    assert refreshed.name == evil_name  # stored raw -- only rendering is escaped


async def test_ui_machine_token_cannot_reach_admin_machines(client, db_session):
    """Machine tokens never authenticate the UI -- only owner sessions
    (app/ui_auth.py). A bare bearer header on a /ui/* route has nothing to
    do with the cookie-based session, so it's treated as unauthenticated.
    """
    machine_headers = await _machine_headers(db_session)
    resp = await client.get("/ui/admin/machines", headers=machine_headers, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"


async def test_ui_revoke_machine_works(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    mint_resp = await client.post("/ui/admin/machines", data={"name": "to-be-revoked", "csrf_token": csrf})
    assert mint_resp.status_code == 201
    machine = (await db_session.execute(Machine.__table__.select().where(Machine.name == "to-be-revoked"))).first()
    machine_id = machine.id

    resp = await client.post(f"/ui/admin/machines/{machine_id}/revoke", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/admin/machines"

    listing = await client.get("/ui/admin/machines")
    assert "revoked" in listing.text.lower()

    from app.models import Machine as MachineModel

    refreshed = await db_session.get(MachineModel, machine_id)
    assert refreshed.status == "revoked"


async def test_ui_revoke_unknown_machine_404(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    resp = await client.post("/ui/admin/machines/01UNKNOWNIDXXXXXXXXXXXXXX/revoke", data={"csrf_token": csrf})
    assert resp.status_code == 404


# --- proposals ---


async def _deposit_proposal(client, headers, title="A proposal", body="rationale") -> str:
    resp = await client.post(
        "/v1/deposits",
        json={
            "deposit_id": str(ULID()),
            "tool": "t",
            "session": "s",
            "project": "brain",
            "reason": "manual",
            "client_ts": "2026-08-06T12:00:00Z",
            "events": [],
            "knowledge": [{"title": title, "namespace": "reference", "body": body, "doctrine_proposal": True}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["knowledge"][0]["id"]


async def test_ui_proposal_approve_records_decision(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers, title="Approve me")
    await _login(client, db_session)

    page = await client.get("/ui/admin/proposals")
    assert page.status_code == 200
    assert "Approve me" in page.text
    csrf = _extract_csrf(page.text)

    resp = await client.post(f"/ui/admin/proposals/{proposal_id}/approve", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303

    from app.models import KnowledgeEntry

    entry = await db_session.get(KnowledgeEntry, proposal_id)
    assert entry.proposal_decision == "approved"
    assert entry.proposal_decided_at is not None

    after = await client.get("/ui/admin/proposals")
    assert "approved" in after.text.lower()


async def test_ui_proposal_reject_records_decision(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers, title="Reject me")
    await _login(client, db_session)

    page = await client.get("/ui/admin/proposals")
    csrf = _extract_csrf(page.text)

    resp = await client.post(f"/ui/admin/proposals/{proposal_id}/reject", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303

    from app.models import KnowledgeEntry

    entry = await db_session.get(KnowledgeEntry, proposal_id)
    assert entry.proposal_decision == "rejected"


async def test_ui_proposal_already_decided_rejected(client, db_session):
    machine_headers = await _machine_headers(db_session)
    proposal_id = await _deposit_proposal(client, machine_headers)
    await _login(client, db_session)

    page = await client.get("/ui/admin/proposals")
    csrf = _extract_csrf(page.text)

    first = await client.post(f"/ui/admin/proposals/{proposal_id}/approve", data={"csrf_token": csrf})
    assert first.status_code == 303

    second = await client.post(f"/ui/admin/proposals/{proposal_id}/reject", data={"csrf_token": csrf})
    assert second.status_code == 422


# --- reserved built-in-librarian machine: legible, not a silent no-op
# (independent review advisory G) ---


async def test_admin_machines_marks_reserved_librarian_row_as_system(client, db_session):
    await _login(client, db_session)
    db_session.add(
        Machine(
            id=librarian_engine_module.LIBRARIAN_MACHINE_ID,
            name=librarian_engine_module.LIBRARIAN_MACHINE_NAME,
            token_hash=hash_token("unused-reserved-machine-token"),
            status="active",
            role="solo",
        )
    )
    await db_session.commit()

    resp = await client.get("/ui/admin/machines")
    assert resp.status_code == 200
    assert "brainard-librarian" in resp.text
    assert '<span class="badge system">system</span>' in resp.text
    assert "Revoking it disables the built-in librarian" in resp.text


async def test_admin_machines_hides_onboarding_prompt_affordance_for_reserved_librarian_row(client, db_session):
    await _login(client, db_session)
    db_session.add(
        Machine(
            id=librarian_engine_module.LIBRARIAN_MACHINE_ID,
            name=librarian_engine_module.LIBRARIAN_MACHINE_NAME,
            token_hash=hash_token("unused-reserved-machine-token"),
            status="active",
            role="solo",
        )
    )
    await db_session.commit()

    resp = await client.get("/ui/admin/machines")
    assert resp.status_code == 200
    # the note explaining why replaces the onboarding-prompt controls
    assert "No rename/role/default-project or onboarding-prompt controls for this row" in resp.text
    # and no prompt-copy affordance was rendered for this specific row's id
    assert f'id="prompt-{librarian_engine_module.LIBRARIAN_MACHINE_ID}"' not in resp.text


async def test_admin_machines_revoke_control_still_present_for_reserved_librarian_row(client, db_session):
    """The row must stay a genuinely useful, visible control -- not hidden --
    since revoking it now really does disable the built-in librarian
    (app/librarian_engine.py's `run_librarian`).
    """
    await _login(client, db_session)
    db_session.add(
        Machine(
            id=librarian_engine_module.LIBRARIAN_MACHINE_ID,
            name=librarian_engine_module.LIBRARIAN_MACHINE_NAME,
            token_hash=hash_token("unused-reserved-machine-token"),
            status="active",
            role="solo",
        )
    )
    await db_session.commit()

    resp = await client.get("/ui/admin/machines")
    assert f'action="/ui/admin/machines/{librarian_engine_module.LIBRARIAN_MACHINE_ID}/revoke"' in resp.text
    assert ">Revoke<" in resp.text


async def test_admin_machines_ordinary_machine_still_shows_onboarding_details(client, db_session):
    """Regression guard: the reserved-row special-casing must not affect
    ordinary machines' existing rename/role/default-project/onboarding-prompt
    affordance.
    """
    await _login(client, db_session)
    await client.post("/ui/admin/machines", data={"name": "ordinary-machine", "csrf_token": _extract_csrf((await client.get("/ui/admin/machines")).text)})

    resp = await client.get("/ui/admin/machines")
    assert "Rename / role / default project / onboarding prompt" in resp.text
    assert "No rename/role/default-project or onboarding-prompt controls for this row" not in resp.text


# --- reactivate: the symmetric UI control for a revoked row, since
# revocation is not a one-way door ---


async def test_admin_machines_reactivate_button_shown_only_for_revoked_rows(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    create_resp = await client.post("/ui/admin/machines", data={"name": "revoke-me", "csrf_token": csrf})
    assert create_resp.status_code == 201
    machine = (await db_session.scalars(select(Machine).where(Machine.name == "revoke-me"))).one()

    before = await client.get("/ui/admin/machines")
    assert f'action="/ui/admin/machines/{machine.id}/revoke"' in before.text
    assert f'action="/ui/admin/machines/{machine.id}/reactivate"' not in before.text

    revoke_resp = await client.post(f"/ui/admin/machines/{machine.id}/revoke", data={"csrf_token": csrf})
    assert revoke_resp.status_code == 303

    after = await client.get("/ui/admin/machines")
    assert f'action="/ui/admin/machines/{machine.id}/reactivate"' in after.text
    assert f'action="/ui/admin/machines/{machine.id}/revoke"' not in after.text
    assert ">Reactivate<" in after.text


async def test_admin_machines_reactivate_actually_reactivates(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    await client.post("/ui/admin/machines", data={"name": "revoke-then-reactivate", "csrf_token": csrf})
    machine = (await db_session.scalars(select(Machine).where(Machine.name == "revoke-then-reactivate"))).one()

    await client.post(f"/ui/admin/machines/{machine.id}/revoke", data={"csrf_token": csrf})
    await db_session.refresh(machine)
    assert machine.status == "revoked"

    reactivate_resp = await client.post(f"/ui/admin/machines/{machine.id}/reactivate", data={"csrf_token": csrf})
    assert reactivate_resp.status_code == 303

    await db_session.refresh(machine)
    assert machine.status == "active"

    listing = await client.get("/ui/admin/machines")
    assert f'action="/ui/admin/machines/{machine.id}/revoke"' in listing.text  # Revoke control is back


async def test_admin_machines_reactivate_without_csrf_rejected(client, db_session):
    await _login(client, db_session)
    page = await client.get("/ui/admin/machines")
    csrf = _extract_csrf(page.text)

    await client.post("/ui/admin/machines", data={"name": "csrf-guard-machine", "csrf_token": csrf})
    machine = (await db_session.scalars(select(Machine).where(Machine.name == "csrf-guard-machine"))).one()
    await client.post(f"/ui/admin/machines/{machine.id}/revoke", data={"csrf_token": csrf})

    resp = await client.post(f"/ui/admin/machines/{machine.id}/reactivate", data={})
    assert resp.status_code == 403

    await db_session.refresh(machine)
    assert machine.status == "revoked"  # unchanged
