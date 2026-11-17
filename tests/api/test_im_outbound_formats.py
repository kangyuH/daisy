import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api import im_payload
from app.infra.lark.cli import LarkCliError
from app.services.im import outbound


ENDPOINTS = ("send", "reply", "respond")
CLI_RESULT = {"data": {"message_id": "om_outbound_test"}}
MARKDOWN = "**结论**\n\n- 验收通过"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aefkAAAAASUVORK5CYII="
)
CARD = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "**结论**"}]}}


@pytest.fixture()
def mock_cli(monkeypatch):
    cli = AsyncMock(return_value=CLI_RESULT)
    monkeypatch.setattr(outbound, "run_cli_async", cli)
    return cli


def _destination(client, endpoint):
    if endpoint == "send":
        return {"chat_id": "oc_outbound_test", "bot_id": "selected-bot"}
    if endpoint == "reply":
        return {
            "message_id": "om_original_test",
            "reply_in_thread": True,
            "bot_id": "selected-bot",
        }
    with patch("app.api.queue.add_typing_reaction", new=AsyncMock(return_value="reaction_test")):
        response = client.post(
            "/queue/inbound/enqueue",
            json={
                "payload": {
                    "bot_id": "inbound-bot",
                    "message_id": "om_original_test",
                    "thread_id": "omt_outbound_test",
                    "sender_open_id": "ou_sender",
                }
            },
        )
    assert response.status_code == 200, response.text
    return {"inbound_id": response.json()["item"]["id"]}


def _flag(args, name):
    return args[args.index(name) + 1]


def _assert_destination(args, endpoint):
    assert _flag(args, "--as") == "bot"
    assert _flag(args, "--profile") == ("inbound-bot" if endpoint == "respond" else "selected-bot")
    if endpoint == "send":
        assert args[:2] == ["im", "+messages-send"]
        assert _flag(args, "--chat-id") == "oc_outbound_test"
        assert "--reply-in-thread" not in args
    else:
        assert args[:2] == ["im", "+messages-reply"]
        assert _flag(args, "--message-id") == "om_original_test"
        assert "--reply-in-thread" in args


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_outbound_renders_markdown_and_mentions(client, mock_cli, endpoint):
    target = _destination(client, endpoint)
    response = client.post(
        f"/im/{endpoint}",
        json={
            **target,
            "message": {"type": "markdown", "text": MARKDOWN},
            "mention_open_ids": ["ou_additional"],
        },
    )

    assert response.status_code == 200, response.text
    mock_cli.assert_awaited_once()
    args = mock_cli.await_args.args[0]
    _assert_destination(args, endpoint)
    assert _flag(args, "--msg-type") == "post"
    mentions = '<at user_id="ou_additional"></at>'
    if endpoint == "respond":
        assert _flag(args, "--idempotency-key") == f"inbound:{target['inbound_id']}"
        mentions = '<at user_id="ou_sender"></at> ' + mentions
    content = json.loads(_flag(args, "--content"))
    assert content == {"zh_cn": {"content": [[{"tag": "md", "text": f"{mentions}\n{MARKDOWN}"}]]}}


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    ("message", "native_type", "native_content"),
    [
        ({"type": "image", "image_key": "img_v3_test"}, "image", {"image_key": "img_v3_test"}),
        ({"type": "file", "file_key": "file_v2_test"}, "file", {"file_key": "file_v2_test"}),
        ({"type": "audio", "file_key": "file_v2_test"}, "audio", {"file_key": "file_v2_test"}),
        ({"type": "video", "file_key": "file_v2_test"}, "media", {"file_key": "file_v2_test"}),
        (
            {"type": "video", "file_key": "file_v2_test", "image_key": "img_v3_cover"},
            "media",
            {"file_key": "file_v2_test", "image_key": "img_v3_cover"},
        ),
        ({"type": "card", "content": CARD}, "interactive", CARD),
    ],
)
def test_outbound_sends_one_native_message(
    client, mock_cli, endpoint, message, native_type, native_content
):
    target = _destination(client, endpoint)
    response = client.post(f"/im/{endpoint}", json={**target, "message": message})

    assert response.status_code == 200, response.text
    assert response.json()["result"] == CLI_RESULT
    mock_cli.assert_awaited_once()
    args = mock_cli.await_args.args[0]
    _assert_destination(args, endpoint)
    assert _flag(args, "--msg-type") == native_type
    assert json.loads(_flag(args, "--content")) == native_content
    assert "--text" not in args


def test_outbound_preserves_legacy_plain_response(client, mock_cli):
    target = _destination(client, "respond")
    response = client.post(
        "/im/respond",
        json={**target, "text": "交付完成", "mention_open_ids": ["ou_sender", "ou_other"]},
    )

    assert response.status_code == 200, response.text
    args = mock_cli.await_args.args[0]
    _assert_destination(args, "respond")
    mentions = '<at user_id="ou_sender"></at> <at user_id="ou_other"></at>'
    assert _flag(args, "--text") == f"{mentions}\n交付完成"
    assert _flag(args, "--idempotency-key") == f"inbound:{target['inbound_id']}"


@pytest.mark.parametrize(
    "invalid_content",
    [
        {"text": "旧内容", "message": {"type": "markdown", "text": MARKDOWN}},
        {"message": {"type": "image", "image_key": "https://example.org/sensitive.png"}},
        {"message": {"type": "file", "file_key": "../report.txt"}},
        {"message": {"type": "video", "file_key": "file_v2_test", "image_key": "/cover.png"}},
        {"message": {"type": "card", "content": json.dumps(CARD)}},
        {"message": {"type": "audio"}},
        {"message": {"type": "unknown", "file_key": "file_v2_test"}},
        {"message": {"type": "audio", "file_key": "file_v2_test"}, "mention_open_ids": ["ou_user"]},
    ],
)
def test_outbound_rejects_invalid_json_before_cli(client, mock_cli, invalid_content):
    response = client.post("/im/send", json={"chat_id": "oc_outbound_test", **invalid_content})

    assert response.status_code == 422, response.text
    mock_cli.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    ("kind", "filename", "content"),
    [("image", "plot.png", PNG), ("file", "报告.txt", b"release report\n")],
)
def test_outbound_upload_uses_target_bot_and_cleans_file(
    client, monkeypatch, endpoint, kind, filename, content
):
    task = client.post(
        "/tasks",
        json={"title": "交付附件", "kind": "readonly", "chat_id": "oc_outbound_test"},
    )
    assert task.status_code == 200, task.text
    task_id = task.json()["task"]["id"]
    target = _destination(client, endpoint)
    staged_dirs = []

    def run_cli(args, *, cwd):
        _assert_destination(args, endpoint)
        assert _flag(args, "--idempotency-key") == "delivery:test"
        path = _flag(args, f"--{kind}")
        assert path.startswith("./")
        directory = Path(cwd)
        staged_dirs.append(directory)
        staged = directory / path
        assert staged.name == filename
        assert staged.resolve().parent == directory.resolve()
        assert staged.read_bytes() == content
        return CLI_RESULT

    monkeypatch.setattr(outbound, "run_cli", run_cli)
    payload = {**target, "task_id": task_id, "idempotency_key": "delivery:test"}
    response = client.post(
        f"/im/{endpoint}",
        data={"type": kind, "payload": json.dumps(payload)},
        files={"file": (filename, content)},
    )

    assert response.status_code == 200, response.text
    assert response.json()["task_link"] == {
        "task_id": task_id,
        "message_id": "om_outbound_test",
        "status": "recorded",
    }
    assert len(staged_dirs) == 1
    assert not staged_dirs[0].exists()


def test_outbound_upload_cleans_file_after_cli_error(client, monkeypatch):
    staged_dirs = []

    def run_cli(args, *, cwd):
        directory = Path(cwd)
        staged_dirs.append(directory)
        staged_file = (directory / _flag(args, "--file")).resolve()
        assert staged_file.parent == directory.resolve()
        assert staged_file.read_bytes() == b"report"
        raise LarkCliError("upload failed", returncode=1, stdout="", stderr="rejected")

    monkeypatch.setattr(outbound, "run_cli", run_cli)
    response = client.post(
        "/im/send",
        data={"type": "file", "payload": '{"chat_id":"oc_outbound_test"}'},
        files={"file": ("../report.txt", b"report")},
    )

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["stderr"] == "rejected"
    assert len(staged_dirs) == 1
    assert not staged_dirs[0].exists()


@pytest.mark.parametrize(
    ("kind", "payload", "size", "expected_status"),
    [
        ("image", '{"chat_id":"oc_outbound_test"}', 0, 422),
        ("video", '{"chat_id":"oc_outbound_test"}', 1, 422),
        ("file", '{"chat_id":"oc_outbound_test","text":"conflicting text"}', 1, 422),
        ("file", "{}", 1, 422),
        ("image", '{"chat_id":"oc_outbound_test"}', 10 * 1024**2 + 1, 413),
        ("file", '{"chat_id":"oc_outbound_test"}', 30 * 1024**2 + 1, 413),
    ],
)
def test_outbound_upload_rejects_invalid_requests_before_cli(
    client, mock_cli, monkeypatch, kind, payload, size, expected_status
):
    upload_cli = Mock()
    monkeypatch.setattr(outbound, "run_cli", upload_cli)
    response = client.post(
        "/im/send",
        data={"type": kind, "payload": payload},
        files={"file": ("report.txt", b"x" * size)},
    )

    assert response.status_code == expected_status, response.text
    upload_cli.assert_not_called()
    mock_cli.assert_not_awaited()


def test_outbound_upload_bounds_request_without_content_length(client, monkeypatch):
    monkeypatch.setattr(im_payload, "_MAX_UPLOAD_BODY", 512)
    upload_cli = Mock()
    monkeypatch.setattr(outbound, "run_cli", upload_cli)
    multipart = client.build_request(
        "POST",
        "/im/send",
        data={"type": "file", "payload": '{"chat_id":"oc_outbound_test"}'},
        files={"file": ("report.txt", b"x" * 512)},
    )
    body = multipart.read()
    request = client.build_request(
        "POST",
        "/im/send",
        headers={"Content-Type": multipart.headers["Content-Type"]},
        content=(body[offset:offset + 128] for offset in range(0, len(body), 128)),
    )
    assert "content-length" not in request.headers

    response = client.send(request)

    assert response.status_code == 413, response.text
    upload_cli.assert_not_called()


def test_outbound_upload_checks_auth_before_parsing(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN", "outbound-test-token")
    response = client.post(
        "/im/send",
        headers={"Content-Type": "multipart/form-data"},
        content=b"malformed",
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_outbound_openapi_advertises_json_and_upload(client, endpoint):
    document = client.get("/openapi.json").json()
    content = document["paths"][f"/im/{endpoint}"]["post"]["requestBody"]["content"]
    assert set(content) == {"application/json", "multipart/form-data"}

    schema = content["multipart/form-data"]["schema"]
    if "$ref" in schema:
        schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    assert set(schema["required"]) == {"payload", "type", "file"}
