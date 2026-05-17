"""Tests for API input validation."""

from __future__ import annotations


def _client(fresh_store):
    import server
    return server.app.test_client()


def test_api_memory_post_valid(fresh_store):
    client = _client(fresh_store)
    resp = client.post("/api/memory", json={
        "text": "This is a test memory",
        "name": "valid name",
        "type": "feedback",
        "tags": "one,two,three",
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["ok"] is True
    assert data["memory"]["text"] == "This is a test memory"
    assert data["memory"]["tags"] == ["one", "two", "three"]


def test_api_memory_post_name_too_long(fresh_store):
    client = _client(fresh_store)
    resp = client.post("/api/memory", json={
        "text": "This is a test memory",
        "name": "a" * 201,
        "type": "feedback",
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "name" in data["error"]


def test_api_memory_post_invalid_type(fresh_store):
    client = _client(fresh_store)
    resp = client.post("/api/memory", json={
        "text": "This is a test memory",
        "type": "invalid_type",
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "type" in data["error"]


def test_api_memory_post_too_many_tags_string(fresh_store):
    client = _client(fresh_store)
    tags = ",".join(f"tag{i}" for i in range(21))
    resp = client.post("/api/memory", json={
        "text": "This is a test memory",
        "tags": tags,
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "tags" in data["error"]


def test_api_memory_post_too_many_tags_list(fresh_store):
    client = _client(fresh_store)
    tags = [f"tag{i}" for i in range(21)]
    resp = client.post("/api/memory", json={
        "text": "This is a test memory",
        "tags": tags,
    })
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
    assert "tags" in data["error"]
