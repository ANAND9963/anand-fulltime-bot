"""Thin Claude wrapper. Everything works without a key (heuristics only), Claude just makes it smarter."""
import json
import os
import re

import anthropic

from . import db


def _client(settings):
    key = settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def available(settings):
    return bool(settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))


def web_search_urls(settings, query, max_uses=4):
    """Use Claude's server-side web search to find live job posting URLs. Returns a list of URLs."""
    client = _client(settings)
    if not client:
        return []
    try:
        resp = client.messages.create(
            model=settings.get("llm_model") or "claude-opus-5-5",
            max_tokens=8000,
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
            messages=[{"role": "user", "content":
                       f"Search the web for currently open job postings: {query}\n"
                       "Prefer direct links on boards.greenhouse.io, job-boards.greenhouse.io, jobs.lever.co, "
                       "jobs.ashbyhq.com and *.myworkdayjobs.com. Reply with the posting URLs, one per line."}],
        )
    except anthropic.APIError as e:
        db.log(0, f"Claude web search failed: {e}", "warn")
        return []
    urls = []
    for b in resp.content:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            urls += [r.url for r in b.content if getattr(r, "url", None)]
        elif b.type == "text":
            urls += re.findall(r"https?://[^\s)\]\"'<>]+", b.text)
    return list(dict.fromkeys(urls))


def ask_json(settings, system, prompt, max_tokens=4000, effort="low"):
    """Ask Claude for a JSON object. Returns dict, or None if no key / failure."""
    client = _client(settings)
    if not client:
        return None
    model = settings.get("llm_model") or "claude-opus-5-5"
    try:
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system + "\nReply with a single JSON object only, no prose, no code fences.",
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
        )
        if model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5"):
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except anthropic.APIConnectionError as e:
        db.log(0, f"Claude connection error: {e}", "warn")
    except anthropic.RateLimitError:
        db.log(0, "Claude rate limited", "warn")
    except anthropic.APIStatusError as e:
        db.log(0, f"Claude API error {e.status_code}: {e.message}", "warn")
    except (json.JSONDecodeError, ValueError) as e:
        db.log(0, f"Claude returned non-JSON: {e}", "warn")
    return None
