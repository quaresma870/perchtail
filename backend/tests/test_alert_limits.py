"""At most MAX_ALERTS_PER_OWNER alerts per account, webhooks by https://
only for new and edited alerts, and alerts saved with http:// before that
still sending, flagged in the API and warned about in the log."""

from app import alerts
from app.alerts import deliver_due, queue_delivery
from app.api.alerts import MAX_ALERTS_PER_OWNER
from app.crypto import encrypt_secret
from app.models import Alert
from sqlmodel import select
from structlog.testing import capture_logs

from tests.test_alerts import _alert, _posts, _source, _user
from tests.test_api_alerts import _make_user, client_for  # noqa: F401 (fixture)

HOOK = "https://example.com/hook"
PLAIN_HOOK = "http://example.com/hook?token=s3cret"


def _fill(session, user, count: int) -> None:
    for n in range(count):
        _alert(session, user, name=f"alert {n}")


# -- the ceiling per owner -----------------------------------------------------------------


def test_the_101st_alert_of_an_owner_is_refused(session, client_for):  # noqa: F811
    user = _make_user(session)
    client = client_for(user)
    _fill(session, user, MAX_ALERTS_PER_OWNER - 1)

    hundredth = client.post("/alerts", json={"name": "100", "query": "q", "webhook_url": HOOK})
    assert hundredth.status_code == 201, hundredth.text

    refused = client.post("/alerts", json={"name": "101", "query": "q", "webhook_url": HOOK})
    assert refused.status_code == 422
    assert "100 alerts" in refused.json()["detail"]
    owned = session.exec(select(Alert).where(Alert.user_id == user.id)).all()
    assert len(owned) == MAX_ALERTS_PER_OWNER


def test_the_ceiling_counts_each_owner_alone_and_frees_up_on_delete(
    session, client_for  # noqa: F811
):
    full, other = _make_user(session), _make_user(session)
    _fill(session, full, MAX_ALERTS_PER_OWNER)

    assert (
        client_for(other)
        .post("/alerts", json={"name": "a", "query": "q", "webhook_url": HOOK})
        .status_code
        == 201
    )
    client = client_for(full)
    assert (
        client.post("/alerts", json={"name": "b", "query": "q", "webhook_url": HOOK}).status_code
        == 422
    )
    first = session.exec(select(Alert).where(Alert.user_id == full.id)).first()
    assert client.delete(f"/alerts/{first.id}").status_code == 204
    assert (
        client.post("/alerts", json={"name": "b", "query": "q", "webhook_url": HOOK}).status_code
        == 201
    )


# -- https:// only ---------------------------------------------------------------------------


def test_a_new_alert_with_an_http_webhook_is_refused(session, client_for):  # noqa: F811
    client = client_for(_make_user(session))
    response = client.post("/alerts", json={"name": "a", "query": "q", "webhook_url": PLAIN_HOOK})
    assert response.status_code == 422
    assert "https://" in response.text


def test_an_alert_cant_be_edited_to_an_http_webhook(session, client_for):  # noqa: F811
    client = client_for(_make_user(session))
    created = client.post("/alerts", json={"name": "a", "query": "q", "webhook_url": HOOK}).json()
    response = client.patch(f"/alerts/{created['id']}", json={"webhook_url": PLAIN_HOOK})
    assert response.status_code == 422


def test_an_alert_saved_with_http_is_flagged_and_keeps_its_url_when_edited(
    session, client_for  # noqa: F811
):
    user = _make_user(session)
    client = client_for(user)
    old = _alert(session, user, webhook_url_encrypted=encrypt_secret(PLAIN_HOOK))
    _alert(session, user, name="secure")

    listed = {a["name"]: a for a in client.get("/alerts").json()}
    assert listed["my alert"]["webhook_insecure"] is True
    assert listed["secure"]["webhook_insecure"] is False
    assert "s3cret" not in client.get("/alerts").text

    # Editing something else keeps the URL, and the flag.
    edited = client.patch(f"/alerts/{old.id}", json={"name": "renamed"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["webhook_insecure"] is True
    session.refresh(old)
    assert alerts.webhook_url(old) == PLAIN_HOOK

    # A new https:// URL clears it.
    fixed = client.patch(f"/alerts/{old.id}", json={"webhook_url": HOOK})
    assert fixed.status_code == 200 and fixed.json()["webhook_insecure"] is False


# -- sending to an http:// URL ---------------------------------------------------------------


def test_each_send_to_an_http_webhook_is_warned_about_without_the_url(
    session, monkeypatch, tmp_path
):
    user = _user(session)
    plain = _alert(session, user, webhook_url_encrypted=encrypt_secret(PLAIN_HOOK))
    secure = _alert(session, user, name="secure")
    source = _source(session, tmp_path)
    posted = _posts(monkeypatch)
    for alert in (plain, secure):
        queue_delivery(session, alert, [alerts.Hit(source.id, "src", "app.log", 0, "refused")])
    session.commit()

    with capture_logs() as logs:
        deliver_due(session)

    assert len(posted) == 2
    warnings = [entry for entry in logs if entry["event"] == "alert.webhook_insecure"]
    assert warnings == [
        {"event": "alert.webhook_insecure", "alert_id": plain.id, "log_level": "warning"}
    ]
    assert "s3cret" not in repr(logs)
