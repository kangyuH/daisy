from unittest.mock import AsyncMock

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.main import create_app
from app.services.bots.store import STATUS_READY
from app.services.im import gateway as im


async def test_mcp_gateway_tools_share_rest_tasks_and_feishu_services(db_file, monkeypatch):
    monkeypatch.setenv("GATEWAY_MCP_TOKEN", "mcp-test-token")
    monkeypatch.setenv("GATEWAY_MCP_CHAT_IDS", "oc_allowed")
    monkeypatch.setenv("GATEWAY_TOKEN", "rest-test-token")
    monkeypatch.setenv("AGENT_AUTO_RESEARCH", "false")
    fetch = AsyncMock(
        return_value={
            "anchor": {"message_id": "om_inbound", "chat_id": "oc_allowed"},
            "messages": [{"message_id": "om_parent", "chat_id": "oc_allowed"}],
            "meta": {"mode": "thread"},
        }
    )
    send = AsyncMock(return_value={"message_id": "om_reply"})
    monkeypatch.setattr(im, "fetch_context", fetch)
    monkeypatch.setattr(im, "respond_to_message", send)

    app = create_app(testing=True)
    base_url = "http://localhost"
    async with app.router.lifespan_context(app):
        app.state.bot_store.upsert_bot(
            bot_id="business",
            app_id="cli_business",
            app_secret="test-secret",
            name="Business Bot",
            enabled=True,
            status=STATUS_READY,
        )
        app.state.bot_store.bind_chat("business", "oc_allowed")
        app.state.bot_store.bind_chat("business", "oc_other")
        inbound = await app.state.queue.enqueue(
            "inbound",
            {
                "message_id": "om_inbound",
                "bot_id": "business",
                "chat_id": "oc_allowed",
                "thread_id": "omt_thread",
                "reply_to_message_id": "om_parent",
            },
        )
        outside = await app.state.queue.enqueue(
            "inbound",
            {"message_id": "om_other", "bot_id": "business", "chat_id": "oc_other"},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=base_url,
            headers={"Authorization": "Bearer rest-test-token"},
        ) as rest, httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
            created = await rest.post(
                "/tasks",
                json={"title": "Investigate", "kind": "readonly", "chat_id": "oc_allowed"},
            )
            assert created.status_code == 200
            task_id = created.json()["task"]["id"]
            hidden = await rest.post(
                "/tasks",
                json={"title": "Other task", "kind": "readonly", "chat_id": "oc_other"},
            )
            assert hidden.status_code == 200
            hidden_id = hidden.json()["task"]["id"]

            assert app.state.message_task_store.record_outbound("om_inbound", task_id)
            assert app.state.message_task_store.record_outbound("om_parent", hidden_id)
            app.state.dispatch_run_store.create_run(
                inbound_id=inbound["id"],
                task_id=hidden_id,
                decision="followup",
                reason="Cross-chat task linkage",
            )
            url = f"{base_url}/mcp"
            assert (await http.post(url, json={})).status_code == 401
            http.headers["Authorization"] = "Bearer mcp-test-token"
            transport = streamable_http_client(url, http_client=http)
            async with Client(transport) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "list_tasks",
                    "get_task",
                    "get_message_context",
                    "append_task_followup",
                    "reply_to_inbound",
                }

                listing = await client.call_tool("list_tasks", {"chat_id": "oc_allowed"})
                assert not listing.is_error
                assert [task["id"] for task in listing.structured_content["tasks"]] == [task_id]
                detail = await client.call_tool("get_task", {"task_id": task_id})
                assert not detail.is_error
                assert detail.structured_content["task"]["title"] == "Investigate"

                context = await client.call_tool("get_message_context", {"inbound_id": inbound["id"]})
                assert not context.is_error
                assert context.structured_content["reply_target"]["message_id"] == "om_parent"
                assert context.structured_content["reply_target"]["task_refs"] == []
                assert [ref["task_id"] for ref in context.structured_content["anchor"]["task_refs"]] == [task_id]
                assert fetch.await_args.kwargs["thread_id"] == "omt_thread"

                note = {"task_id": task_id, "message": "MCP update", "idempotency_key": "note-1"}
                first = await client.call_tool("append_task_followup", note)
                retried = await client.call_tool("append_task_followup", note)
                assert not first.is_error and not retried.is_error
                assert first.structured_content["event"]["id"] == retried.structured_content["event"]["id"]
                events = (await rest.get(f"/tasks/{task_id}")).json()["task"]["events"]
                assert [event["message"] for event in events].count("MCP update") == 1

                reply = await client.call_tool(
                    "reply_to_inbound",
                    {"inbound_id": inbound["id"], "message": {"type": "markdown", "text": "**Done**"}},
                )
                assert not reply.is_error
                assert reply.structured_content["result"]["message_id"] == "om_reply"
                assert "task_link" not in reply.structured_content
                assert send.await_args.kwargs["profile"] == "business"
                assert send.await_args.kwargs["message_id"] == "om_inbound"
                assert send.await_args.kwargs["idempotency_key"] == f"inbound:{inbound['id']}"
                assert send.await_args.kwargs["message"]["type"] == "markdown"

                hidden_task = await client.call_tool("get_task", {"task_id": hidden_id})
                missing_task = await client.call_tool("get_task", {"task_id": hidden_id + 1})
                assert hidden_task.is_error and missing_task.is_error
                assert hidden_task.content == missing_task.content

                denied_reply = await client.call_tool(
                    "reply_to_inbound", {"inbound_id": outside["id"], "text": "Forbidden"}
                )
                missing_reply = await client.call_tool(
                    "reply_to_inbound", {"inbound_id": outside["id"] + 1, "text": "Forbidden"}
                )
                assert denied_reply.is_error and missing_reply.is_error
                assert denied_reply.content == missing_reply.content
                send.assert_awaited_once()
