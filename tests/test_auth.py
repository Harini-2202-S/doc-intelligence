import jwt

from tests.conftest import PASSWORD


async def test_register_login_me(client, register_and_login):
    headers = await register_and_login()
    r = await client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["email"] == "asha@example.com"
    assert "password_hash" not in r.json()


async def test_email_is_case_insensitive(client, register_and_login):
    await register_and_login(email="Asha@Example.com")
    r = await client.post("/auth/login", json={"email": "asha@example.com", "password": PASSWORD})
    assert r.status_code == 200


async def test_short_password_rejected(client):
    r = await client.post("/auth/register", json={"email": "a@example.com", "password": "short"})
    assert r.status_code == 422


async def test_duplicate_email_rejected(client, register_and_login):
    await register_and_login()
    r = await client.post("/auth/register", json={"email": "asha@example.com", "password": PASSWORD})
    assert r.status_code == 409


async def test_wrong_password_and_unknown_email_look_the_same(client, register_and_login):
    await register_and_login()
    wrong = await client.post("/auth/login", json={"email": "asha@example.com", "password": "x" * 12})
    unknown = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 12})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_login_is_rate_limited(client, register_and_login, settings):
    await register_and_login()  # uses one attempt
    codes = [
        (await client.post("/auth/login", json={"email": "asha@example.com", "password": "x" * 12})).status_code
        for _ in range(settings.login_attempts_per_15min - 1)
    ]
    assert set(codes) == {401}
    r = await client.post("/auth/login", json={"email": "asha@example.com", "password": PASSWORD})
    assert r.status_code == 429


async def test_rejects_missing_tampered_and_forged_tokens(client, register_and_login, settings):
    headers = await register_and_login()
    assert (await client.get("/auth/me")).status_code == 401

    token = headers["Authorization"].split()[1]
    tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})).status_code == 401

    payload = jwt.decode(token, options={"verify_signature": False})
    forged = jwt.encode(payload, "attacker-secret-attacker-secret-0000", algorithm="HS256")
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})).status_code == 401

    unsigned = jwt.encode(payload, None, algorithm="none")
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {unsigned}"})).status_code == 401


async def test_delete_account_requires_password_and_removes_everything(client, register_and_login, upload, app):
    headers = await register_and_login()
    doc_id = (await upload(headers)).json()["id"]
    storage = app.state.storage
    assert len(list(storage.root.iterdir())) == 1
    assert app.state.retriever.store.count() > 0

    r = await client.post("/auth/delete-account", headers=headers, json={"password": "x" * 12})
    assert r.status_code == 403

    r = await client.post("/auth/delete-account", headers=headers, json={"password": PASSWORD})
    assert r.status_code == 204
    assert list(storage.root.iterdir()) == []
    assert app.state.retriever.store.count() == 0
    assert (await client.get("/auth/me", headers=headers)).status_code == 401
    assert (await client.get(f"/documents/{doc_id}", headers=headers)).status_code == 401
