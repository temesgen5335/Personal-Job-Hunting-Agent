"""The confirmation card, rendered once for every surface.

A card is built from validated arguments and computed previews — never from anything
the model wrote (R29). The CLI prints it, the dashboard shows it, Telegram sends it and
the MCP puts it in an elicitation dialog; the text is identical because there is one
function. Tools with a computed preview get a branch here; every other tool gets the
policy's `describes` line followed by its arguments.

A result that starts with `REFUSED:` means "do not offer this at all" — the caller
must treat it as a no, never as a card to approve.
"""

from __future__ import annotations


def render_card(name: str, args: dict, policy, settings, store) -> str:
    args = args or {}
    if name == "apply_config_change":
        from jobagent.assistant.config_policy import ConfigRefused, preview
        try:
            return preview(str(args.get("field", "")), str(args.get("value", "")),
                           settings, store).render()
        except ConfigRefused as exc:
            return f"REFUSED: {exc}"

    described = getattr(policy, "describes", "") or ""
    lines = [described] if described else []
    lines += [f"{k}: {v}" for k, v in args.items()]
    return "\n".join(lines)
