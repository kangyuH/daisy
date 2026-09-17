from __future__ import annotations

from typing import Any

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import BaseTool

from workers.dispatcher.prompts import SYSTEM_PROMPT


def build_agent_executor(
    llm: BaseChatModel,
    tools: list[BaseTool],
    *,
    max_iterations: int = 6,
) -> AgentExecutor:
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ]
    )
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(
        agent=agent,
        tools=tools,
        max_iterations=max_iterations,
        early_stopping_method="force",
        handle_parsing_errors=True,
        verbose=False,
        return_intermediate_steps=True,
    )


def run_dispatcher_agent(
    executor: AgentExecutor,
    *,
    user_input: str,
) -> dict[str, Any]:
    return executor.invoke({"input": user_input})
