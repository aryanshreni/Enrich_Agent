"""Thin tool-calling loop over a Foundation Model API endpoint, traced with MLflow.

Usage:
    agent = ToolAgent(endpoint="databricks-claude-sonnet-5-5", tools=[...])
    result = agent.run(system_prompt, user_message, response_model=ReviewPacket)

Each tool is a plain Python callable plus a JSON schema for its arguments. Tools must be
read-only (UC metadata, read-only SQL, library and glossary lookups) or write only to the
artifact Volume; data changes happen in the executor after approval, never here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import mlflow
from databricks.sdk import WorkspaceClient
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class ToolAgent:
    endpoint: str
    tools: list[Tool] = field(default_factory=list)
    max_turns: int = 30
    max_repair_attempts: int = 2
    # databricks-claude-sonnet-5-5 rejects the temperature parameter, so it is only sent when set.
    temperature: float | None = None
    max_tokens: int = 16000

    def __post_init__(self) -> None:
        self._client = WorkspaceClient().serving_endpoints.get_open_ai_client()
        self._by_name = {t.name: t for t in self.tools}

    @mlflow.trace(span_type="TOOL")
    def _call_tool(self, name: str, arguments: str) -> str:
        tool = self._by_name.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            args = json.loads(arguments or "{}")
            out = tool.fn(**args)
            return out if isinstance(out, str) else json.dumps(out, default=str)
        except Exception as e:  # tool errors go back to the model so it can recover
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def _complete(self, messages: list[dict[str, Any]], use_tools: bool) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.endpoint,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if use_tools and self.tools:
            kwargs["tools"] = [t.spec() for t in self.tools]
        return self._client.chat.completions.create(**kwargs)

    @mlflow.trace(span_type="AGENT")
    def run(self, system_prompt: str, user_message: str, response_model: type[T] | None = None) -> T | str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        for _ in range(self.max_turns):
            msg = self._complete(messages, use_tools=True).choices[0].message
            if msg.tool_calls:
                messages.append(msg.model_dump(exclude_none=True))
                for call in msg.tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": self._call_tool(call.function.name, call.function.arguments),
                    })
                continue
            content = msg.content or ""
            if response_model is None:
                return content
            return self._parse_with_repair(messages, content, response_model)
        raise RuntimeError(f"agent did not finish within {self.max_turns} turns")

    def _parse_with_repair(self, messages: list[dict[str, Any]], content: str, model: type[T]) -> T:
        """Validate the final answer against the schema; on failure, send the errors back to the model."""
        for attempt in range(self.max_repair_attempts + 1):
            try:
                return model.model_validate_json(_strip_fences(content))
            except ValidationError as e:
                if attempt == self.max_repair_attempts:
                    raise
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "Your output failed validation. Fix every error and return only "
                                                "the corrected JSON.\n" + e.json(include_url=False)},
                ]
                content = self._complete(messages, use_tools=False).choices[0].message.content or ""
        raise AssertionError("unreachable")


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
