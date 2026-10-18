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
"""

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

SYSTEM_PROMPT = """You are the finance assistant of an ERPNext desk. The logged-in user asks what is open for them and how to get through it.

Rules:
- You can only read. Nothing here can create, change, submit or pay anything. If the user asks for that, say this assistant cannot act yet.
- Say what is open only from the DATA below. It was read from ERPNext with the user's own permissions; what it does not list, they cannot see or it does not exist. Never invent a document name or a figure.
- DATA counts are complete; DATA top lists hold at most the first ten of each. Say "at least" when a count is larger than its list.
- Answer in JSON, exactly this shape and nothing else:
  {"text": "...", "items": [{"doctype": "...", "name": "..."}]}
  text: your answer in the user's language, plain text, short lines, no markdown headings or tables. items: every document you name, each once, so the desk can link it. An item's doctype and name must be copied from DATA unchanged."""


class AssistantError(Exception):
    """A failed call or an unusable answer. The message names the status, never the key or a response body."""


def answer(
    key, model, max_tokens, message, history, data, opener=urllib.request.urlopen
):
    """One round trip: the question, the past turns and the collected data to OpenRouter, the parsed answer back.

    history: the earlier turns as [{"role", "content"}], already trimmed by the caller. data: the collectors' result,
    read with the user's own permissions (assistant.collect). Raises AssistantError when the call or the answer fails;
    the key stays in the request headers and in nothing raised.
    """
    request = urllib.request.Request(
        URL,
        data=json.dumps(request_body(model, max_tokens, message, history, data)).encode(
            "utf-8"
        ),
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
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
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
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise AssistantError("OpenRouter returned an unusable response")
    return parse_answer(message["content"])


def request_body(model, max_tokens, message, history, data):
    """The request body: the model, the prompt with the DATA block, the past turns, the question, the privacy routing."""
    return {
        "model": model,
        "messages": (
            [{"role": "system", "content": system_prompt(data)}]
            + list(history)
            + [{"role": "user", "content": message}]
        ),
        "max_tokens": max_tokens,
        "provider": dict(PROVIDER),
    }


def system_prompt(data):
    """The system prompt with the collectors' result as the DATA block."""
    return (
        SYSTEM_PROMPT
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
    if items is None:
        items = []
    if not isinstance(items, list) or not isinstance(parsed.get("text") or "", str):
        raise AssistantError("OpenRouter's answer was not the JSON asked for")
    return {"text": parsed.get("text") or "", "items": items}


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
