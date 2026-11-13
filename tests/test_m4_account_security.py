"""M4 -- account security and staff onboarding.

Password change, admin-issued password resets, invitation-only staff
accounts, admin account management, session revocation, and the audit rows
for all of them. MySQL races (code redemption, last-admin) live in
tests/test_mysql_concurrency.py.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

import pytest
from flask import g

from app.extensions import db as _db
from app.models.account_token import AccountToken, TokenPurpose
from app.models.audit_log import AuditEvent, AuditLog
from app.models.user import Role, User
from app.services.accounts import (
    AccountError,
    CodeRejected,
    invite_staff,
    issue_password_reset,
    redeem_code,
    set_active,
)
from app.utils.ratelimit import login_limiter
from app.utils.security import needs_rehash, verify_password
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def _installation_identity(installation_identity):
    """O2: staff invitations (and create_admin) need the installation
    identity a migrated database always has; create_all() builds none."""
    return installation_identity


PASSWORD = "correct-horse-1"
NEW_PASSWORD = "brand-new-pass-42"


@pytest.fixture(autouse=True)
def _clean_login_limiter():
    login_limiter._attempts.clear()
    yield
    login_limiter._attempts.clear()


def req(client, method, url, **kw):
    """Each request as its own identity (known issue #29)."""
    g.pop("current_user", None)
    return getattr(client, method)(url, **kw)


def user(username, role=Role.CUSTOMER, **kw):
    return make_user(username=username, email=f"{username}@example.com", password=PASSWORD, role=role, **kw)


def login(client, username, password=PASSWORD, portal=""):
    return req(client, "post", f"/login/{portal}" if portal else "/login",
               data={"username": username, "password": password})


def signed_in(app, username, role=Role.CUSTOMER):
    account = user(username, role)
    client = app.test_client()
    assert login(client, username).status_code == 302
    return client, account


def is_signed_in(client) -> bool:
    return req(client, "get", "/_test/me").status_code == 200


def fresh(pk) -> User:
    _db.session.expire_all()
    return _db.session.get(User, pk)


def audit(event: AuditEvent) -> list[AuditLog]:
    _db.session.expire_all()
    return _db.session.query(AuditLog).filter_by(event_type=event).all()


def all_audit_text() -> str:
    _db.session.expire_all()
    return " ".join(f"{r.metadata_json} {r.user_agent}" for r in _db.session.query(AuditLog))


def code_from(response) -> str:
    match = re.search(r'id="one-time-code">([^<]+)<', response.get_data(as_text=True))
    assert match, "no one-time code on the page"
    return match.group(1)


def redeem(client, code, password=NEW_PASSWORD, confirm=None):
    return req(client, "post", "/account/setup",
               data={"code": code, "password": password, "password_confirm": confirm or password})


# =============================================================================
# self-service password change
# =============================================================================


def change(client, current=PASSWORD, new=NEW_PASSWORD, confirm=None):
    return req(client, "post", "/account/password",
               data={"current_password": current, "password": new, "password_confirm": confirm or new})


@pytest.mark.parametrize("role", list(Role))
def test_password_change_works_for_every_role(app, role):
    client, account = signed_in(app, "member", role)
    old_hash = account.password_hash
    assert change(client).status_code == 302
    stored = fresh(account.id).password_hash
    assert stored != old_hash and stored.startswith("$argon2id$") and not needs_rehash(stored)
    assert verify_password(stored, NEW_PASSWORD) and not verify_password(stored, PASSWORD)
    assert is_signed_in(client)  # this session survives
    other = app.test_client()
    assert login(other, "member").status_code == 200  # old password refused
    assert login(other, "member", NEW_PASSWORD).status_code == 302
    [row] = audit(AuditEvent.PASSWORD_CHANGED)
    assert (row.user_id, row.success, row.metadata_json) == (account.id, True, None)


def test_password_change_signs_out_other_sessions(app):
    client, account = signed_in(app, "member")
    elsewhere = app.test_client()
    login(elsewhere, "member")
    assert is_signed_in(elsewhere)
    change(client)
    assert not is_signed_in(elsewhere) and is_signed_in(client)


def test_wrong_current_password_changes_nothing(app):
    client, account = signed_in(app, "member")
    before = account.password_hash
    response = change(client, current="not-my-password-9")
    assert response.status_code == 200 and "current password is incorrect" in response.get_data(as_text=True)
    assert fresh(account.id).password_hash == before
    [row] = audit(AuditEvent.PASSWORD_CHANGED)
    assert row.success is False and json.loads(row.metadata_json) == {"reason": "wrong_current_password"}


def test_wrong_current_password_is_rate_limited(app):
    client, account = signed_in(app, "member")
    statuses = [change(client, current=f"guess-number-{i}").status_code for i in range(7)]
    assert 429 in statuses
    assert change(client).status_code == 429  # even the right password, while limited
    assert verify_password(fresh(account.id).password_hash, PASSWORD)


@pytest.mark.parametrize("new,confirm", [
    ("short1", None),                 # too short
    ("onlyletterspassword", None),    # no digit
    ("password123", None),            # common
    (NEW_PASSWORD, "different-pass-42"),
    (PASSWORD, None),                 # unchanged
    ("x1" * 70, None),                # longer than the maximum
])
def test_invalid_new_password_is_refused(app, new, confirm):
    client, account = signed_in(app, "member")
    before = account.password_hash
    change(client, new=new, confirm=confirm)
    assert fresh(account.id).password_hash == before
    assert [r for r in audit(AuditEvent.PASSWORD_CHANGED) if r.success] == []


def test_password_change_requires_login(client, db):
    assert req(client, "get", "/account/password").status_code == 401
    assert req(client, "post", "/account/password", data={"current_password": "x"}).status_code == 401


def test_plaintext_passwords_never_leave_the_server(app, caplog):
    caplog.set_level("DEBUG")
    client, account = signed_in(app, "member")
    page = change(client, current="wrong-pass-123")
    change(client)
    assert PASSWORD not in page.get_data(as_text=True) and "wrong-pass-123" not in page.get_data(as_text=True)
    for secret in (PASSWORD, NEW_PASSWORD, "wrong-pass-123", fresh(account.id).password_hash):
        assert secret not in caplog.text and secret not in all_audit_text()


# =============================================================================
# staff invitations
# =============================================================================


@pytest.fixture
def admin_client(app, db):
    return signed_in(app, "boss", Role.ADMIN)


def invite(client, username="newcook", email=None, **extra):
    return req(client, "post", "/admin/accounts/invite",
               data={"username": username, "email": email or f"{username}@example.com", **extra})


def test_admin_invites_and_staff_completes_setup(app, admin_client):
    client, boss = admin_client
    page = invite(client)
    assert page.status_code == 200
    code = code_from(page)
    cook = _db.session.query(User).filter_by(username="newcook").one()
    assert (cook.role, cook.is_active, cook.staff_approved) == (Role.STAFF, False, True)

    staff = app.test_client()
    assert login(staff, "newcook", NEW_PASSWORD, "staff").status_code == 200  # not usable yet
    assert redeem(staff, code).status_code == 302
    cook = fresh(cook.id)
    assert cook.is_active and verify_password(cook.password_hash, NEW_PASSWORD)
    assert login(staff, "newcook", NEW_PASSWORD, "staff").status_code == 302
    assert req(staff, "get", "/staff/orders").status_code == 200

    [invited] = audit(AuditEvent.STAFF_INVITED)
    assert invited.user_id == boss.id and json.loads(invited.metadata_json) == {"target_user_id": cook.id}
    [accepted] = audit(AuditEvent.STAFF_INVITATION_ACCEPTED)
    assert accepted.user_id == cook.id and accepted.metadata_json is None


def test_invitation_role_cannot_be_escalated(admin_client):
    client, _ = admin_client
    invite(client, "sneaky", role="admin", is_admin="1", staff_approved="0", is_active="1")
    account = _db.session.query(User).filter_by(username="sneaky").one()
    assert (account.role, account.is_active) == (Role.STAFF, False)


@pytest.mark.parametrize("username,email", [("x", "ok@example.com"), ("bad name!", "ok@example.com"),
                                            ("goodname", "not-an-email"), ("boss", "other@example.com"),
                                            ("other", "boss@example.com")])
def test_invalid_or_duplicate_invitations_create_nothing(admin_client, username, email):
    client, _ = admin_client
    response = invite(client, username, email)
    assert 'id="one-time-code"' not in response.get_data(as_text=True)
    assert _db.session.query(User).count() == 1 and _db.session.query(AccountToken).count() == 0


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.STAFF])
def test_non_admins_cannot_invite(app, role):
    client, _ = signed_in(app, "member", role)
    assert req(client, "get", "/admin/accounts/invite").status_code == 403
    assert invite(client).status_code == 403
    assert _db.session.query(User).filter_by(username="newcook").count() == 0


def test_anonymous_cannot_invite(client, db):
    assert invite(client).status_code == 401
    assert _db.session.query(User).count() == 0


def test_public_staff_registration_is_gone(client, db):
    for method in ("get", "post"):
        assert req(client, method, "/register/staff", data={
            "username": "walkin", "email": "walkin@example.com",
            "password": PASSWORD, "password_confirm": PASSWORD}).status_code == 404
    req(client, "post", "/register", data={"username": "walkin", "email": "walkin@example.com",
                                           "password": PASSWORD, "password_confirm": PASSWORD, "role": "staff"})
    assert _db.session.query(User).filter_by(username="walkin").one().role == Role.CUSTOMER


def test_codes_are_random_and_only_their_hash_is_stored(admin_client):
    client, _ = admin_client
    codes = [code_from(invite(client, f"cook{i}")) for i in range(5)]
    assert len(set(codes)) == 5 and all(len(c) >= 43 for c in codes)  # 256 bits, url-safe
    stored = {t.token_hash for t in _db.session.query(AccountToken)}
    assert stored == {hashlib.sha256(c.encode()).hexdigest() for c in codes}
    for code in codes:
        assert code not in all_audit_text() and code not in stored


def test_code_is_single_use(app, admin_client):
    client, _ = admin_client
    code = code_from(invite(client))
    staff = app.test_client()
    redeem(staff, code)
    again = redeem(staff, code, password="another-pass-77")
    assert "invalid or has expired" in again.get_data(as_text=True)
    cook = _db.session.query(User).filter_by(username="newcook").one()
    assert verify_password(fresh(cook.id).password_hash, NEW_PASSWORD)
    [rejected] = audit(AuditEvent.ACCOUNT_TOKEN_REJECTED)
    assert rejected.user_id is None and json.loads(rejected.metadata_json) == {"purpose": "staff_invite"}


def test_expired_code_is_refused(app, admin_client):
    client, _ = admin_client
    code = code_from(invite(client))
    token = _db.session.query(AccountToken).one()
    token.expires_at = token.created_at - timedelta(seconds=1)
    _db.session.commit()
    assert "invalid or has expired" in redeem(app.test_client(), code).get_data(as_text=True)
    assert fresh(token.user_id).is_active is False


@pytest.mark.parametrize("code", ["", "nope", "A" * 43, "x" * 500, "' OR 1=1 --"])
def test_unknown_codes_are_refused(client, db, code):
    response = redeem(client, code)
    assert 'Set password' in response.get_data(as_text=True)
    assert _db.session.query(AuditLog).filter(AuditLog.success.is_(True)).count() == 0


def test_bad_password_does_not_burn_the_code(app, admin_client):
    client, _ = admin_client
    code = code_from(invite(client))
    staff = app.test_client()
    redeem(staff, code, password="short")
    assert _db.session.query(AccountToken).one().used_at is None
    assert redeem(staff, code).status_code == 302


def test_new_invitation_supersedes_and_service_refuses_non_admin(admin_client):
    client, boss = admin_client
    cook = user("cook", Role.STAFF)
    with pytest.raises(AccountError):
        invite_staff(cook, "another", "another@example.com")
    with pytest.raises(AccountError):
        issue_password_reset(cook, boss)


def test_setup_page_refuses_signed_in_users(app):
    client, _ = signed_in(app, "member")
    assert req(client, "get", "/account/setup").status_code == 302


# =============================================================================
# admin-issued password reset
# =============================================================================


def test_admin_reset_flow(app, admin_client):
    client, boss = admin_client
    target_client, target = signed_in(app, "member")
    page = req(client, "post", f"/admin/accounts/{target.id}/reset")
    code = code_from(page)
    assert not is_signed_in(target_client)                  # sessions ended when the code was issued
    assert login(app.test_client(), "member").status_code == 302  # old password still works until redeemed

    redeem(app.test_client(), code)
    assert verify_password(fresh(target.id).password_hash, NEW_PASSWORD)
    assert login(app.test_client(), "member").status_code == 200
    assert login(app.test_client(), "member", NEW_PASSWORD).status_code == 302

    [issued] = audit(AuditEvent.PASSWORD_RESET_ISSUED)
    assert issued.user_id == boss.id and json.loads(issued.metadata_json) == {"target_user_id": target.id}
    [done] = audit(AuditEvent.PASSWORD_RESET)
    assert done.user_id == target.id and code not in all_audit_text()


def test_reset_code_is_never_flashed_or_redirected(admin_client):
    client, _ = admin_client
    target = user("member")
    page = req(client, "post", f"/admin/accounts/{target.id}/reset")
    assert page.status_code == 200 and page.headers.get("Cache-Control") == "no-store"
    code = code_from(page)
    with client.session_transaction() as session:
        assert code not in json.dumps(session.get("_flashes", []))


def test_reset_completion_revokes_sessions_started_after_issue(app, admin_client):
    client, _ = admin_client
    target = user("member")
    code = code_from(req(client, "post", f"/admin/accounts/{target.id}/reset"))
    meanwhile = app.test_client()
    login(meanwhile, "member")
    redeem(app.test_client(), code)
    assert not is_signed_in(meanwhile)


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.STAFF])
def test_non_admins_cannot_reset(app, role):
    client, _ = signed_in(app, "member", role)
    victim = user("victim")
    assert req(client, "post", f"/admin/accounts/{victim.id}/reset").status_code == 403
    assert _db.session.query(AccountToken).count() == 0


def test_anonymous_cannot_reset(client, db):
    victim = user("victim")
    assert req(client, "post", f"/admin/accounts/{victim.id}/reset").status_code == 401
    assert _db.session.query(AccountToken).count() == 0


def test_reset_refused_for_self_unknown_and_deactivated(admin_client):
    client, boss = admin_client
    gone = user("gone", is_active=False)
    for target in (boss.id, gone.id):
        assert 'id="one-time-code"' not in req(client, "post", f"/admin/accounts/{target}/reset",
                                               headers={"Accept": "text/html"}).get_data(as_text=True)
    assert req(client, "post", "/admin/accounts/999999/reset").status_code == 404
    assert _db.session.query(AccountToken).count() == 0


def test_reissuing_a_reset_voids_the_previous_code(admin_client):
    client, _ = admin_client
    target = user("member")
    first = code_from(req(client, "post", f"/admin/accounts/{target.id}/reset"))
    second = code_from(req(client, "post", f"/admin/accounts/{target.id}/reset"))
    with pytest.raises(CodeRejected):
        redeem_code(first, NEW_PASSWORD, NEW_PASSWORD)
    redeem_code(second, NEW_PASSWORD, NEW_PASSWORD)


# =============================================================================
# account management, activation, revocation
# =============================================================================


def test_account_list_is_admin_only(app, client, db):
    assert req(client, "get", "/admin/accounts").status_code == 401
    for role in (Role.CUSTOMER, Role.STAFF):
        member, _ = signed_in(app, f"m_{role.value}", role)
        assert req(member, "get", "/admin/accounts").status_code == 403
    boss, _ = signed_in(app, "boss", Role.ADMIN)
    page = req(boss, "get", "/admin/accounts").get_data(as_text=True)
    assert "m_customer" in page and "m_staff" in page and "boss" in page
    assert "$argon2" not in page


def test_deactivation_ends_sessions_and_blocks_login(app, admin_client):
    client, boss = admin_client
    member_client, member = signed_in(app, "member", Role.STAFF)
    assert req(client, "post", f"/admin/accounts/{member.id}/deactivate").status_code == 302
    assert not is_signed_in(member_client)
    assert req(member_client, "get", "/staff/orders").status_code == 401
    assert login(app.test_client(), "member").status_code == 200
    [row] = audit(AuditEvent.ACCOUNT_DEACTIVATED)
    assert row.user_id == boss.id and json.loads(row.metadata_json) == {"target_user_id": member.id,
                                                                        "role": "staff"}


def test_reactivation_does_not_revive_old_sessions_or_codes(app, admin_client):
    client, _ = admin_client
    member_client, member = signed_in(app, "member")
    code = code_from(req(client, "post", f"/admin/accounts/{member.id}/reset"))
    login(member_client, "member")
    req(client, "post", f"/admin/accounts/{member.id}/deactivate")
    req(client, "post", f"/admin/accounts/{member.id}/activate")
    assert fresh(member.id).is_active
    assert not is_signed_in(member_client)          # the cookie from before deactivation stays dead
    with pytest.raises(CodeRejected):
        redeem_code(code, NEW_PASSWORD, NEW_PASSWORD)  # the reset code died with the deactivation
    assert login(app.test_client(), "member").status_code == 302
    assert len(audit(AuditEvent.ACCOUNT_ACTIVATED)) == 1


def test_deactivated_invitation_cannot_be_accepted(app, admin_client):
    client, _ = admin_client
    code = code_from(invite(client))
    cook = _db.session.query(User).filter_by(username="newcook").one()
    req(client, "post", f"/admin/accounts/{cook.id}/activate")
    req(client, "post", f"/admin/accounts/{cook.id}/deactivate")
    assert "invalid or has expired" in redeem(app.test_client(), code).get_data(as_text=True)
    assert fresh(cook.id).is_active is False


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.STAFF])
def test_non_admins_cannot_change_activation(app, role):
    client, _ = signed_in(app, "member", role)
    victim = user("victim")
    assert req(client, "post", f"/admin/accounts/{victim.id}/deactivate").status_code == 403
    assert fresh(victim.id).is_active


def test_activation_is_post_only_and_takes_no_role_field(admin_client):
    client, _ = admin_client
    member = user("member")
    assert req(client, "get", f"/admin/accounts/{member.id}/deactivate").status_code == 405
    req(client, "post", f"/admin/accounts/{member.id}/activate", data={"role": "admin", "staff_approved": "1"})
    assert fresh(member.id).role == Role.CUSTOMER


def test_admin_cannot_deactivate_self(admin_client):
    client, boss = admin_client
    other = user("other_admin", Role.ADMIN)
    req(client, "post", f"/admin/accounts/{boss.id}/deactivate")
    assert fresh(boss.id).is_active and fresh(other.id).is_active


def test_last_active_admin_cannot_be_deactivated(db):
    """The sequential form of the mutual-deactivation race: A deactivates B,
    then B -- acting on a stale page -- tries to deactivate A. A is now the
    last active admin, so the service refuses, whatever the actor's state."""
    a, b = user("admin_a", Role.ADMIN), user("admin_b", Role.ADMIN)
    assert set_active(a, b, False) is True
    with pytest.raises(AccountError) as exc:
        set_active(fresh(b.id), fresh(a.id), False)
    assert "other active administrator" in exc.value.errors["account"][0]
    assert fresh(a.id).is_active and not fresh(b.id).is_active


def test_last_admin_rule_over_http(app):
    boss_client, boss = signed_in(app, "boss", Role.ADMIN)
    other_client, other = signed_in(app, "other", Role.ADMIN)
    req(boss_client, "post", f"/admin/accounts/{other.id}/deactivate")
    assert fresh(other.id).is_active is False
    req(boss_client, "post", f"/admin/accounts/{boss.id}/deactivate")  # self: refused
    assert fresh(boss.id).is_active


def test_admin_management_change_rolls_back_when_audit_fails(admin_client, monkeypatch):
    client, _ = admin_client
    member = user("member")

    def boom(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr("app.routes.admin_accounts.record_event", boom)
    assert req(client, "post", f"/admin/accounts/{member.id}/deactivate").status_code == 500
    assert fresh(member.id).is_active
    assert req(client, "post", f"/admin/accounts/{member.id}/reset").status_code == 500
    assert _db.session.query(AccountToken).count() == 0
    assert invite(client).status_code == 500
    assert _db.session.query(User).filter_by(username="newcook").count() == 0


def test_code_redemption_rolls_back_when_audit_fails(app, admin_client, monkeypatch):
    client, _ = admin_client
    code = code_from(invite(client))

    def boom(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr("app.routes.account.record_event", boom)
    assert redeem(app.test_client(), code).status_code == 500
    token = _db.session.query(AccountToken).one()
    _db.session.refresh(token)
    assert token.used_at is None and fresh(token.user_id).is_active is False


def test_password_change_rolls_back_when_audit_fails(app, monkeypatch):
    client, account = signed_in(app, "member")
    before = account.password_hash

    def boom(*_a, **_k):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr("app.routes.account.record_event", boom)
    assert change(client).status_code == 500
    assert fresh(account.id).password_hash == before


def test_audit_rows_never_contain_secrets(app, admin_client):
    client, _ = admin_client
    code = code_from(invite(client))
    redeem(app.test_client(), code)
    member = user("member")
    reset_code = code_from(req(client, "post", f"/admin/accounts/{member.id}/reset"))
    redeem(app.test_client(), reset_code)
    text = all_audit_text()
    hashes = [t.token_hash for t in _db.session.query(AccountToken)]
    for secret in [code, reset_code, NEW_PASSWORD, PASSWORD, *hashes, *(u.password_hash for u in
                                                                       _db.session.query(User))]:
        assert secret not in text


def test_token_purpose_vocabulary_is_enforced(db):
    assert {p.value for p in TokenPurpose} == {"staff_invite", "password_reset"}
