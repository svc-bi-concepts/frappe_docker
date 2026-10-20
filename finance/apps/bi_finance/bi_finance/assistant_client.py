"""OpenRouter chat completions, read only: one question and the collected data go out, one structured answer comes back.

Standard library only, no frappe import: the tests run without the image, as wise_client's do. The API key goes in the
Authorization header of the request and nowhere else: not in a URL, an error, a log or an answer. An error names the
HTTP status, never the response body, which may name the account. The routing asks OpenRouter for endpoints that keep
nothing (provider.zdr) and do not train on the traffic (provider.data_collection deny), with no fallback to one that
would; if no such endpoint serves the model, the error says so through its HTTP status and the settings are wrong.

The model is told to answer JSON: {"text": "...", "items": [{"doctype": "...", "name": "..."}]}. parse_answer reads
that shape leniently (a fenced block is fine, prose around the object too) and hands the dict on; the caller validates
each item against the database before anything is linked. Only POST of a question and its data; nothing here writes
anywhere in ERPNext, so a key that can only chat is enough.

Tool calling (erp-wcf3, part 2b): when the caller passes tools (OpenAI format, built from the action registry), they go
in the request with tool_choice auto and the system prompt swaps its read-only rule for the action rules. The tool
calls the model returns come back unjudged as "calls" ({"name", "arguments"}); the caller sends each one through
propose(), which refuses what it does not know. Nothing here decides what an action may do.
"""

import http.client
import json
import re
import urllib.error
import urllib.request

URL = "https://openrouter.ai/api/v1/chat/completions"

# the bead's cap: an answer arrives inside this or the call is given up on
TIMEOUT_SECONDS = 30

# zero-retention routing, Benchi's standing rule (decision 2026-10-18): no endpoint that keeps
# or trains on the traffic, and no silent fallback to one that does
PROVIDER = {"allow_fallbacks": False, "zdr": True, "data_collection": "deny"}

FENCES = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

# the bookkeeper's task ids (erp-00z0): the collectors are keyed by them, and a tool call names the one it serves
TASK_IDS = (
    "bank.lines",
    "bank.feeds",
    "sales.overdue",
    "drafts",
    "bills.due",
    "expenses.approve",
    "month.revaluation",
    "month.close",
    "vat.quarter",
    "payroll.month",
)

# the tool calls one answer may carry into propose(); more are dropped, so one reply cannot flood the log
MAX_CALLS = 10

READ_ONLY_RULE = "- You can only read. Nothing here can create, change, submit or pay anything. If the user asks for that, say this assistant cannot act yet."

# replaces READ_ONLY_RULE when tools are offered
ACTION_RULE = """- You can read, and you can propose actions, only through the tools offered; there is nothing else you can do. The desk checks every proposal and decides alone whether it runs at once (with Undo), waits for the user's click or needs an approver, so never say an action is done: say it was proposed. DATA's ai_actions lists the user's AI Actions of the last 24 hours with their state: never propose again an action listed there as proposed, auto-done, approved or approved-dry-run (same type and target).
- Each tool call must name the task it serves (task: one of the task ids of DATA) and give your confidence (confidence: 0 to 1) that the action is right and DATA backs it. A low confidence is better than a guess. Propose nothing the user did not ask for or the task does not call for. DATA lists, under proposals, the tools a task may use."""

# added to the action rule while the ledger date has not passed
DRY_RUN_RULE = "- Until the date in the first line of your answer, bookings only run as dry run: an action with a ledger effect shows what it would book and books nothing."

SYSTEM_PROMPT = (
    """You are the finance assistant of an ERPNext desk. The logged-in user asks what is open for them and how to get through it.

Rules:
"""
    + READ_ONLY_RULE
    + """
- Say what is open only from the DATA below. It was read from ERPNext with the user's own permissions; what it does not list, they cannot see or it does not exist. Never invent a document name or a figure.
- When the user asks what is open for them, group the answer under the task ids of the task table (DATA's key of each entry: """
    + ", ".join(TASK_IDS)
    + """): one group per task id with something open, headed by the task id, in that order; the user's own todos come last.
- DATA counts are complete; DATA top lists hold at most the first ten of each. Say "at least" when a count is larger than its list.
- Answer in JSON, exactly this shape and nothing else:
  {"text": "...", "items": [{"doctype": "...", "name": "..."}]}
  text: your answer in the user's language, plain text, short lines, no markdown headings or tables. items: every document you name, each once, so the desk can link it. An item's doctype and name must be copied from DATA unchanged."""
)


class AssistantError(Exception):
    """A failed call or an unusable answer. The message names the status, never the key or a response body."""


def answer(
    key,
    model,
    max_tokens,
    message,
    history,
    data,
    banner=None,
    tools=None,
    opener=urllib.request.urlopen,
):
    """One round trip: the question, the past turns and the collected data to OpenRouter, the parsed answer back.

    history: the earlier turns as [{"role", "content"}], already trimmed by the caller. data: the collectors' result,
    read with the user's own permissions (assistant.collect). banner: the line every answer must start with while the
    ledger date has not passed, else None. tools: the OpenAI-format tool list of the registry, or None to offer none (the
    kill switch off). Raises AssistantError when the call or the answer fails; the key stays in
    the request headers and in nothing raised.
    """
    request = urllib.request.Request(
        URL,
        data=json.dumps(
            request_body(model, max_tokens, message, history, data, banner, tools)
        ).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as err:
        raise AssistantError(f"HTTP {err.code} from OpenRouter") from None
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        http.client.HTTPException,
        OSError,
    ) as err:
        # a network failure or a body that is not JSON; the reason is a class name, the text may carry the URL
        raise AssistantError(
            f"no usable answer from OpenRouter ({type(err).__name__})"
        ) from None
    if not isinstance(reply, dict):
        raise AssistantError("OpenRouter returned an unusable response")
    if reply.get("error"):
        # OpenRouter answers routing and credit problems as a success code with an error object;
        # its text may name the account, so the message stays general
        raise AssistantError("OpenRouter refused the request")
    choices = reply.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AssistantError("OpenRouter returned an unusable response")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AssistantError("OpenRouter returned an unusable response")
    calls = tool_calls(message) if tools else []
    content = message.get("content")
    if calls and not isinstance(content, str):
        # a reply of tool calls alone carries no content: the caller words the text
        return {"text": "", "items": [], "calls": calls}
    if not isinstance(content, str):
        raise AssistantError("OpenRouter returned an unusable response")
    if calls:
        try:
            parsed = parse_answer(content)
        except AssistantError:
            # the proposals matter more than the model's wording of them
            parsed = {"text": "", "items": []}
        return {**parsed, "calls": calls}
    return {**parse_answer(content), "calls": []}


def tool_calls(message):
    """The tool calls of a reply as [{"name", "arguments"}], at most MAX_CALLS; arguments is a dict.

    Arguments the model sent as text are read as JSON; text that is none, or JSON that is no object, becomes an empty
    dict, which the schema check of propose() refuses and logs. A call without a usable name is kept with the empty
    name, which propose() refuses as an action it does not know.
    """
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []
    calls = []
    for call in raw[:MAX_CALLS]:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            function = {}
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        calls.append(
            {
                "name": name if isinstance(name, str) else "",
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )
    return calls


def request_body(model, max_tokens, message, history, data, banner=None, tools=None):
    """The request body: the model, the prompt with the DATA block, the past turns, the question, the privacy routing.

    With tools, the tool list and tool_choice auto join it; the provider settings are the same on every request.
    """
    body = {
        "model": model,
        "messages": (
            [{"role": "system", "content": system_prompt(data, banner, bool(tools))}]
            + list(history)
            + [{"role": "user", "content": message}]
        ),
        "max_tokens": max_tokens,
        "provider": dict(PROVIDER),
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return body


def system_prompt(data, banner=None, tools=False):
    """The system prompt with the banner rule and the collectors' result as the DATA block.

    tools: actions are offered, so the read-only rule gives way to the action rules (and, before the ledger date, to
    the dry run rule).
    """
    head = SYSTEM_PROMPT
    if tools:
        head = head.replace(READ_ONLY_RULE, ACTION_RULE)
        if banner:
            head += "\n" + DRY_RUN_RULE
    if banner:
        head += '\n\nStart every answer with this exact first line: "' + banner + '"'
    return (
        head
        + "\n\nDATA, read now with the user's own permissions:\n"
        + json.dumps(data, default=str)
    )


def parse_answer(content):
    """The model's content as {"text", "items"}: JSON as asked, fenced or wrapped, else AssistantError.

    items is handed on as it came; the caller validates each entry against the database, so a shape the desk cannot
    link (a text where the list was asked for) fails here rather than linking nothing quietly.
    """
    body = FENCES.sub("", (content or "").strip())
    parsed = _json_object(body)
    if parsed is None:
        raise AssistantError("OpenRouter's answer was not the JSON asked for")
    items = parsed.get("items")
    text = parsed.get("text")
    if not isinstance(items, list) or not isinstance(text, str) or not text.strip():
        raise AssistantError("OpenRouter's answer was not the JSON asked for")
    return {"text": text, "items": items}


def _json_object(body):
    """The JSON object in the content: the whole text, else the text between the first brace and the last."""
    for candidate in (body, body[body.find("{") : body.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
