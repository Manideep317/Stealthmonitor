"""
agent.py
Agent + human-in-the-loop controller for Stealth Monitor's onboarding flow.

Wraps discover.py / selector.py's existing functions as tools the LLM can
call. The model decides when it's confident enough to act on its own
(register a target directly) versus when it should call ask_human to check
with you first — the goal is you get asked less often than the fully
scripted selector.py, and only when there's genuine ambiguity (unclear
tier names, marketplace listings, low-confidence extraction).

selector.py is NOT replaced by this file — keep it as the deterministic
fallback if the agent loop misbehaves live during a demo (LLM flake,
malformed tool call, etc).

Uses Featherless's native OpenAI-compatible tool calling. Kimi-K2-Instruct
is confirmed to support this natively (tagged "Tools"/"Function Calling"
in Featherless's own model catalog) — some other model families on
Featherless only fake tool calling via prompted JSON, so don't swap MODEL
without checking that first.
"""
import asyncio
import json
import sys

import requests

import config
import discover as discovery
import selector
import watcher
from db import DB

db = DB()

API_URL = f"{config.FEATHERLESS_BASE_URL}/chat/completions"
MODEL = "moonshotai/Kimi-K2-Instruct"  # confirmed native tool-calling support

SYSTEM_PROMPT = """You are the onboarding agent for Stealth Monitor, a price-tracking tool.

Your job: given a niche or company name from the user, find real competitors,
find their product/pricing pages, and register the ones worth monitoring.

You have tools to search for competitors, discover and validate product
pages, register a target, and ask the human a question when you're unsure.

Rules:
- Call search_competitors first, always, before anything else.
- Don't ask the human to pick from the competitor list — use your own
  judgment on which look like real, relevant competitors for the niche.
  Skip anything that's obviously not a real operating business.
- Never register more than 5 products for a single company without asking
  the human first — that's usually a sign you found a whole catalog, not a
  specific set of products worth tracking individually.
- If a company's site is a marketplace listing (indiamart, amazon,
  alibaba, etsy, etc.) rather than their own storefront, ask the human
  whether they still want it tracked — don't decide either way.
- If product validation returns a price but the tier/product name is
  "unknown" or missing, ask the human to confirm before registering it —
  a price with no label is not useful in an alert.
- If validation finds nothing at all for a company, ask the human for a
  manual URL rather than giving up silently.
- Be decisive when confidence is high (clear name, clear price, clear
  product page) — only use ask_human for genuine ambiguity, not every step.
- When you're done with all companies worth tracking, stop calling tools
  and reply in plain text summarizing what got registered and why anything
  was skipped.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_competitors",
            "description": (
                "Search for competitor companies in a given niche, or "
                "similar to a named company. Returns candidates with "
                "name, url, location, description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "niche_or_company": {
                        "type": "string",
                        "description": "Niche description or seed company name",
                    },
                },
                "required": ["niche_or_company"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_products",
            "description": (
                "For a given company domain, find and validate product/"
                "pricing pages that have real extractable prices. Tries "
                "sitemap crawl, homepage link crawl, and pricing-path "
                "probing, then validates each candidate via JSON-LD, meta "
                "tags, or a rendered pricing table. Returns validated "
                "products/tiers with their prices, plus whether the "
                "domain looks like a third-party marketplace listing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Company homepage URL"},
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_target",
            "description": (
                "Register a URL as a watch target in the database, so "
                "watcher.py will start polling it for price/availability "
                "changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_human",
            "description": (
                "Ask the human a clarifying question when confidence is "
                "low. Use sparingly — only for genuine ambiguity, not "
                "routine steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional short list of suggested answers, shown "
                            "to the human alongside free-text input."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
]


def chat_completion_request(messages: list[dict], tools: list[dict] | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.FEATHERLESS_API_KEY}",
    }
    json_data = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    if tools is not None:
        json_data["tools"] = tools

    response = requests.post(API_URL, headers=headers, json=json_data, timeout=60)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------
# Tool execution — thin wrappers around discover.py / selector.py / db.py.
# Each returns a JSON-serializable result to hand back to the model.
# ---------------------------------------------------------------------
def tool_search_competitors(niche_or_company: str) -> dict:
    candidates = discovery.find_competitors(niche_or_company)
    return {"candidates": candidates, "count": len(candidates)}


async def tool_discover_products(domain: str) -> dict:
    domain = selector.normalize_url(domain)
    urls = await selector.find_product_candidates(domain)
    validated = await selector.validate_product_candidates(urls)
    return {
        "domain": domain,
        "is_marketplace": selector.is_marketplace_domain(domain),
        "products": validated,
        "count": len(validated),
    }


def tool_register_target(url: str) -> dict:
    url = selector.normalize_url(url)
    db.add_target(url)
    return {"registered": url}


def ask_human(question: str, options: list[str] | None = None) -> str:
    print(f"\n🤖 {question}")
    if options:
        for i, opt in enumerate(options, start=1):
            print(f"  {i}. {opt}")
        raw = input("Your answer (number or free text): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        return raw
    return input("Your answer: ").strip()


async def execute_tool(name: str, args: dict):
    if name == "search_competitors":
        return tool_search_competitors(**args)
    if name == "discover_products":
        return await tool_discover_products(**args)
    if name == "register_target":
        return tool_register_target(**args)
    if name == "ask_human":
        return ask_human(**args)
    return {"error": f"unknown tool {name}"}


# ---------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------
async def run_agent(user_request: str, max_turns: int = 20) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_request},
    ]

    await watcher.browsermgr.start()
    try:
        for turn in range(max_turns):
            try:
                result = chat_completion_request(messages, tools=TOOLS)
            except requests.exceptions.RequestException as e:
                print(f"[agent] Featherless request failed: {e}")
                return

            choice = result["choices"][0]
            message = choice["message"]
            messages.append(message)

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                # model is done — print its final summary and stop
                print(f"\n{message.get('content', '')}")
                return

            for call in tool_calls:
                fn_name = call["function"]["name"]
                try:
                    fn_args = json.loads(call["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                print(f"[agent] calling {fn_name}({fn_args})")
                try:
                    tool_result = await execute_tool(fn_name, fn_args)
                except Exception as e:
                    tool_result = {"error": str(e)}

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
                })

        print(f"[agent] hit max_turns ({max_turns}) without finishing — stopping.")
    finally:
        await watcher.browsermgr.stop()


# ---------------------------------------------------------------------
# Messaging-context agent loop (Telegram / email inbound)
# ---------------------------------------------------------------------
def _ask_human_messaging(question: str, options: list[str] | None = None) -> str:
    """Replacement for ask_human when running from a messaging context.
    We can't block-wait for user input, so we tell the model to use its
    best judgment.  The question is still logged to the console."""
    print(f"[agent-msg] ask_human (auto-decided): {question}")
    if options:
        # Pick the first option as a reasonable default
        return f"(Auto-decided — no human in the loop) Going with: {options[0]}"
    return "(Auto-decided — no human in the loop) Use your best judgment and proceed."


async def _execute_tool_messaging(name: str, args: dict):
    """Like execute_tool but swaps ask_human for the non-blocking variant."""
    if name == "search_competitors":
        return tool_search_competitors(**args)
    if name == "discover_products":
        return await tool_discover_products(**args)
    if name == "register_target":
        return tool_register_target(**args)
    if name == "ask_human":
        return _ask_human_messaging(**args)
    return {"error": f"unknown tool {name}"}


async def run_agent_for_message(user_request: str, reply_fn, max_turns: int = 20) -> None:
    """Agent loop designed for the messaging context (Telegram / email).

    Instead of printing to stdout / reading from stdin, all user-facing
    output is sent back through *reply_fn* (typically message.reply from
    the Caspian SDK).  ask_human is replaced with an auto-decision stub
    because we can't block-wait for input in a webhook handler.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_request},
    ]

    reply_fn("🔍 Searching for competitors — this may take a minute…")

    await watcher.browsermgr.start()
    try:
        for turn in range(max_turns):
            try:
                result = chat_completion_request(messages, tools=TOOLS)
            except requests.exceptions.RequestException as e:
                print(f"[agent-msg] Featherless request failed: {e}")
                reply_fn(f"⚠️ Sorry, the AI backend is unreachable right now. Please try again in a bit.\n({e})")
                return

            choice = result["choices"][0]
            message = choice["message"]
            messages.append(message)

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                # Model is done — send its final summary back to the user
                final_text = message.get("content", "")
                if final_text:
                    reply_fn(final_text)
                else:
                    reply_fn("✅ Done — no further details from the agent.")
                print(f"[agent-msg] finished: {final_text[:200]}")
                return

            for call in tool_calls:
                fn_name = call["function"]["name"]
                try:
                    fn_args = json.loads(call["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                print(f"[agent-msg] calling {fn_name}({fn_args})")

                # Send a brief progress update for long-running tools
                if fn_name == "search_competitors":
                    reply_fn(f"🔎 Searching competitors for: {fn_args.get('niche_or_company', '…')}")
                elif fn_name == "discover_products":
                    reply_fn(f"🌐 Discovering product pages on: {fn_args.get('domain', '…')}")
                elif fn_name == "register_target":
                    reply_fn(f"✅ Registered for monitoring: {fn_args.get('url', '…')}")

                try:
                    tool_result = await _execute_tool_messaging(fn_name, fn_args)
                except Exception as e:
                    tool_result = {"error": str(e)}

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
                })

        reply_fn("⏱️ Hit the maximum number of steps — stopping here. You can send another message to continue.")
        print(f"[agent-msg] hit max_turns ({max_turns}) without finishing.")
    except Exception as e:
        print(f"[agent-msg] unexpected error: {e}")
        try:
            reply_fn(f"⚠️ Something went wrong: {e}")
        except Exception:
            pass
    finally:
        await watcher.browsermgr.stop()


if __name__ == "__main__":
    user_request = " ".join(sys.argv[1:]).strip()
    if not user_request:
        user_request = input("What niche or company are you tracking competitors for? ").strip()
    if not user_request:
        print("Need a niche to search for.")
        sys.exit(1)
    asyncio.run(run_agent(user_request))