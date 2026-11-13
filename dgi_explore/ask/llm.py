"""The one place that talks to a language model.

Everything else in the ask package sees only this interface, so the model
behind it can be changed by a setting (DGI_ASK_PROVIDER, DGI_ASK_MODEL)
without touching the tools or the loop:

    chat = llm.open_chat(system, tool_specs)
    reply = chat.user("how many people live in Leitrim?")
    while reply.tool_calls:
        reply = chat.tool_results([(call.id, json_text, is_error), ...])
    print(reply.text)

A provider keeps the conversation in its own format (Claude needs its thinking
blocks sent back unchanged; other providers have other rules), which is why
the history lives inside the Chat object and not in the loop.

Providers (decided 13 Nov 2026, see ~/org/20261111_dgi-search-plan.org):
    deepseek, kimi   the defaults: cheap enough to run for the public
    anthropic, openai  only with a key the user brings themselves

A key is looked for in this order: the api_key argument (a user's own key),
the provider's environment variable, ~/.config/dgi-explore/keys.env, then
~/learnarr/.env. To add a provider that speaks the OpenAI chat format, add a
row to OPENAI_COMPATIBLE; for any other, write a class with user() and
tool_results() and register it in PROVIDERS.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROVIDER = "deepseek"
DEFAULT_EFFORT = "high"

# Providers that speak the OpenAI chat-completions format. Checked against each
# provider's own documentation on 13 Nov 2026; model names change often.
OPENAI_COMPATIBLE = {
    "deepseek": {"base_url": "https://api.deepseek.com", "key": "DEEPSEEK_API_KEY",
                 "model": "deepseek-v4-pro"},        # cheaper: deepseek-flash
    "kimi": {"base_url": "https://api.moonshot.ai/v1", "key": "MOONSHOT_API_KEY",
             "model": "kimi-k3.1-pro"},
    "openai": {"base_url": None, "key": "OPENAI_API_KEY", "model": None},   # name a model
}
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5",
                  **{k: v["model"] for k, v in OPENAI_COMPATIBLE.items()}}

# USD per million tokens (input, output), for the cost shown after each answer.
# From Anthropic's price list as of November 2026; check before relying on it.
PRICES = {
    "claude-fable-5-2": (8.00, 40.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    # DeepSeek peak-hour prices for uncached input; off-peak is half, cached far less.
    "deepseek-v4-pro": (1.32, 3.96),
    "deepseek-flash": (0.30, 1.20),
}


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Reply:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    stop: str = ""            # "done", "tools", "refused", "cut_off"
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


KEY_FILES = [Path.home() / ".config/dgi-explore/keys.env", Path.home() / "learnarr/.env"]


def find_key(name, given=None):
    """The API key called `name`: the one given, else the environment, else a key file."""
    if given:
        return given
    if os.environ.get(name):
        return os.environ[name]
    for env in KEY_FILES:
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith(name + "="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        return key
    return None


class AnthropicChat:
    """Claude through the Anthropic API."""

    def __init__(self, system, tool_specs, model, effort, api_key=None):
        import anthropic
        key = find_key("ANTHROPIC_API_KEY", api_key)
        if not key:
            raise SystemExit("no ANTHROPIC_API_KEY: Claude is used only with your own key")
        self.client = anthropic.Anthropic(api_key=key)
        self.model, self.effort = model, effort
        self.system = system
        self.tools = [{"name": t["name"], "description": t["description"],
                       "input_schema": t["input_schema"], "strict": True}
                      for t in tool_specs]
        self.messages = []

    def _send(self):
        import anthropic
        while True:
            try:
                r = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=self.system,
                    tools=self.tools,
                    messages=self.messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort},
                    cache_control={"type": "ephemeral"},
                    # If the model declines a request on policy grounds, let the
                    # API hand it to its default fallback model.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.RateLimitError as e:
                raise RuntimeError(f"the model service is rate limiting us: {e.message}") from e
            except anthropic.APIStatusError as e:
                raise RuntimeError(f"model service error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise RuntimeError(f"could not reach the model service: {e}") from e
            # Keep the whole reply (thinking blocks included) for the next request.
            self.messages.append({"role": "assistant", "content": r.content})
            if r.stop_reason == "pause_turn":
                continue
            break
        u = r.usage
        reply = Reply(
            text="\n".join(b.text for b in r.content if b.type == "text").strip(),
            tool_calls=[ToolCall(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"],
            tokens_in=(u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                      + (getattr(u, "cache_creation_input_tokens", 0) or 0),
            tokens_out=u.output_tokens or 0,
            model=r.model)
        reply.stop = {"tool_use": "tools", "end_turn": "done", "refusal": "refused",
                      "max_tokens": "cut_off"}.get(r.stop_reason, r.stop_reason or "")
        if reply.stop != "tools":
            reply.tool_calls = []
        return reply

    def user(self, text):
        self.messages.append({"role": "user", "content": text})
        return self._send()

    def tool_results(self, results):
        """results: [(tool_call_id, text, is_error)] for every call in the last reply."""
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": bool(err)}
            for i, text, err in results]})
        return self._send()


class OpenAICompatChat:
    """DeepSeek, Kimi, OpenAI: any provider that speaks the OpenAI chat format."""

    def __init__(self, system, tool_specs, model, effort, api_key=None, provider="deepseek"):
        from openai import OpenAI
        cfg = OPENAI_COMPATIBLE[provider]
        key = find_key(cfg["key"], api_key)
        if not key:
            raise SystemExit(f"no {cfg['key']} for provider {provider!r}: put it in the "
                             f"environment or in {KEY_FILES[0]} as {cfg['key']}=...")
        if not model:
            raise SystemExit(f"provider {provider!r} needs a model name (--model or DGI_ASK_MODEL)")
        self.client = OpenAI(api_key=key, base_url=cfg["base_url"])
        self.provider, self.model, self.effort = provider, model, effort
        self.tools = [{"type": "function",
                       "function": {"name": t["name"], "description": t["description"],
                                    "parameters": t["input_schema"]}}
                      for t in tool_specs]
        self.messages = [{"role": "system", "content": system}]

    def _extra(self):
        """How each provider is asked to reason before answering."""
        deep = self.effort in ("high", "xhigh", "max")
        if self.provider == "deepseek":
            return ({"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}
                    if deep else {})
        if self.provider == "kimi":     # accepts low, high, max; its own default is max
            return {"reasoning_effort": "max" if self.effort == "max" else "high" if deep else "low"}
        return {}

    def _send(self):
        import openai
        try:
            r = self.client.chat.completions.create(
                model=self.model, messages=self.messages, tools=self.tools, **self._extra())
        except openai.RateLimitError as e:
            raise RuntimeError(f"the model service is rate limiting us: {e}") from e
        except openai.APIStatusError as e:
            raise RuntimeError(f"model service error {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise RuntimeError(f"could not reach the model service: {e}") from e
        choice = r.choices[0]
        msg = choice.message
        # Send the assistant message back exactly as it came (some providers
        # require their reasoning text to be returned with the tool calls).
        self.messages.append(msg.model_dump(exclude_none=True))
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_bad_json": c.function.arguments}
            calls.append(ToolCall(c.id, c.function.name, args))
        u = r.usage
        reply = Reply(text=(msg.content or "").strip(), tool_calls=calls,
                      tokens_in=(u.prompt_tokens if u else 0) or 0,
                      tokens_out=(u.completion_tokens if u else 0) or 0, model=r.model or self.model)
        reply.stop = ("tools" if calls else
                      {"stop": "done", "length": "cut_off", "content_filter": "refused"}
                      .get(choice.finish_reason, choice.finish_reason or ""))
        return reply

    def user(self, text):
        self.messages.append({"role": "user", "content": text})
        return self._send()

    def tool_results(self, results):
        for i, text, _err in results:
            self.messages.append({"role": "tool", "tool_call_id": i, "content": text})
        return self._send()


PROVIDERS = {"anthropic": AnthropicChat, **{k: OpenAICompatChat for k in OPENAI_COMPATIBLE}}


def open_chat(system, tool_specs, provider=None, model=None, effort=None, api_key=None):
    """api_key: a user's own key, for the providers that are bring-your-own."""
    provider = provider or os.environ.get("DGI_ASK_PROVIDER", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}")
    model = model or os.environ.get("DGI_ASK_MODEL") or DEFAULT_MODELS.get(provider)
    effort = effort or os.environ.get("DGI_ASK_EFFORT", DEFAULT_EFFORT)
    if provider in OPENAI_COMPATIBLE:
        return OpenAICompatChat(system, tool_specs, model, effort, api_key, provider)
    return PROVIDERS[provider](system, tool_specs, model, effort, api_key)


def cost_usd(model, tokens_in, tokens_out):
    """Rough cost; None if the model's price is not listed. Ignores cache discounts."""
    p = PRICES.get(model) or next((v for k, v in PRICES.items() if model.startswith(k)), None)
    return None if not p else (tokens_in * p[0] + tokens_out * p[1]) / 1e6
