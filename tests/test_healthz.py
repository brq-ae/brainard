async def test_healthz_reports_ok_and_database_reachable(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "database": "reachable"}
