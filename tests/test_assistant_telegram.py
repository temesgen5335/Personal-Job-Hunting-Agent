"""The Telegram bridge.

Logic lives in `assistant_bridge` rather than in the handler precisely so it can be
tested without a Telegram runtime — the gap that let an undefined `_llm()` ship in the
`/apply` path. The handler in `app.py` is kept thin enough to read instead.
"""

import pytest

from agentkit.llm.types import ToolCall
from agentkit.session import Surface
from jobagent.assistant import build_assistant
from jobagent.bot.assistant_bridge import (
    Answer,
    PendingBox,
    ask_blocking,
    format_answer,
    format_pending,
    truncate,
)
from jobagent.config import Settings
from jobagent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    return s


# --- the property that matters most on this surface ----------------------------------

def test_a_config_change_cannot_be_approved_from_chat(store):
    """A tap on a phone, no re-auth, a screen too small to read a diff on — against an
    action that rewrites pipeline configuration.

    Structural, not remembered: CHAT is outside `admin_surfaces`, so the refusal comes
    from the gatekeeper rather than from this module choosing to apply a rule.
    """
    a = build_assistant(store=store, settings=Settings(_env_file=None),
                        surface=Surface.CHAT,
                        ask=lambda *args: True)      # even with an unconditional yes
    out = a.toolbox.execute(ToolCall("c1", "apply_config_change",
                                     {"field": "ingest_max_age_days", "value": "30"}))
    assert out.is_error and "cannot be confirmed from chat" in out.content


def test_an_ordinary_action_is_still_confirmable_from_chat(store):
    from jobagent.core.schemas import JobPosting

    job_id = store.upsert_job(JobPosting(title="AI Engineer", company="Acme",
                                         source="remoteok", url="http://x/1"))
    a = build_assistant(store=store, settings=Settings(_env_file=None),
                        surface=Surface.CHAT, ask=lambda *args: True)
    out = a.toolbox.execute(ToolCall("c1", "triage",
                                     {"job_id": job_id, "state": "dismissed"}))
    assert not out.is_error
    assert store.get_triage(job_id)["state"] == "dismissed"


# --- pending approvals ----------------------------------------------------------------

def test_a_nonce_fits_telegrams_callback_payload():
    """Callback data is capped at 64 bytes. A nonce that does not fit silently breaks
    the button rather than erroring anywhere visible."""
    nonce = PendingBox().add("triage", {"job_id": "x", "state": "dismissed"}, "card")
    assert len(f"askok:{nonce}".encode()) <= 64


def test_the_arguments_stay_out_of_the_callback_payload():
    """Only the nonce travels. There is nothing in the button for a tampered client to
    change — the same property the HTTP surface gets, for the same reason."""
    box = PendingBox()
    args = {"job_id": "abc", "state": "dismissed"}
    nonce = box.add("triage", args, "card")
    assert "abc" not in nonce and "dismissed" not in nonce
    assert box.take(nonce)["args"] == args


def test_an_approval_is_single_use_and_expires():
    clock = {"t": 0.0}
    box = PendingBox(now=lambda: clock["t"])
    n1 = box.add("triage", {"job_id": "a"}, "card")
    assert box.take(n1) is not None
    assert box.take(n1) is None                    # single-use

    n2 = box.add("triage", {"job_id": "b"}, "card")
    clock["t"] += 10_000
    assert box.take(n2) is None                    # expired


# --- rendering ---------------------------------------------------------------------------

def test_a_long_answer_is_cut_at_a_line_boundary_and_says_so():
    """Telegram rejects anything over 4096 characters, so the failure would be the whole
    message vanishing rather than a truncated one."""
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(200))
    out = truncate(text)
    assert len(out) < 3700 and out.endswith("…(truncated)")
    assert not out.splitlines()[-3].endswith("x" * 3 + "…")   # cut on a line, not mid-word


def test_degradation_is_stated_in_the_message():
    out = format_answer(Answer(text="all good", provider="openrouter", model="m",
                               strategy="prefetch_single_shot",
                               warnings=("degraded: weaker model",)))
    assert "all good" in out and "openrouter/m" in out
    assert "! degraded: weaker model" in out


def test_an_error_is_reported_plainly_rather_than_as_an_empty_answer():
    assert format_answer(Answer(text="", error="No LLM provider is configured.")) \
        == "⚠️ No LLM provider is configured."


def test_the_confirmation_card_carries_the_computed_diff():
    card = format_pending({"tool": "apply_config_change",
                           "card": "ingest_max_age_days: 0 → 30\nWould have dropped 4 of 6."},
                          dashboard_url="http://localhost:4321/settings")
    assert "0 → 30" in card and "4 of 6" in card
    assert "localhost:4321/settings" in card       # chat points at the dashboard


def test_an_empty_question_is_refused_without_reaching_a_model(tmp_path):
    answer = ask_blocking(db_path=str(tmp_path / "t.db"), settings=Settings(_env_file=None),
                          question="   ", pending_box=PendingBox())
    assert answer.error and not answer.text


def test_no_provider_configured_is_reported_not_crashed(tmp_path, monkeypatch):
    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "QWEN_API_KEY", "CUSTOM_LLM_BASE_URL"):
        monkeypatch.setenv(key, "")
    answer = ask_blocking(db_path=str(tmp_path / "t.db"),
                          settings=Settings(_env_file=None),
                          question="how are things?", pending_box=PendingBox())
    assert "No LLM provider" in answer.error


def test_the_bot_registers_the_ask_command_and_its_buttons():
    """A handler that exists but is never registered is invisible, and the handler tests
    cannot catch that."""
    import inspect

    import jobagent.bot.app as bot
    src = inspect.getsource(bot)
    assert 'CommandHandler("ask", ask_cmd)' in src
    assert '"askok"' in src and '"askno"' in src


# --- addressing the assistant by name ------------------------------------------------

def test_the_assistant_answers_to_its_name_in_plain_chat():
    """"Baer, ..." reaches the assistant the same as /ask, which is what makes the name
    real in chat and not only in the model's own answer."""
    from jobagent.assistant import ASSISTANT_NAME
    from jobagent.bot.assistant_bridge import address

    q = "is the pipeline healthy?"
    assert address(f"{ASSISTANT_NAME}, {q}") == q
    assert address(f"{ASSISTANT_NAME} {q}") == q
    assert address(f"{ASSISTANT_NAME}: {q}") == q
    assert address(f"{ASSISTANT_NAME.lower()} {q}") == q      # case-insensitive
    assert address(f"{ASSISTANT_NAME.upper()} — {q}") == q


def test_a_bare_name_is_an_opening_not_a_question():
    """"Baer" alone should prompt for more, not send an empty question to a model."""
    from jobagent.assistant import ASSISTANT_NAME
    from jobagent.bot.assistant_bridge import address

    assert address(ASSISTANT_NAME) == ""
    assert address(f"{ASSISTANT_NAME}  ") == ""


def test_a_passing_mention_is_not_a_command():
    """Leading address only. A message that merely mentions the name is not addressed
    to the assistant, and hijacking it would make the bot feel like it is interrupting."""
    from jobagent.bot.assistant_bridge import address

    assert address("did Baer answer earlier?") is None
    assert address("the Baermann role looks good") is None   # word boundary, not prefix
    assert address("") is None
    assert address("just a normal message") is None


def test_the_name_is_a_single_constant_shared_by_every_surface():
    """One source of truth, so the prompt, the CLI, Telegram and the dashboard cannot
    disagree about what the assistant is called."""
    from jobagent.assistant import ASSISTANT_NAME
    from jobagent.assistant.manifest import SYSTEM_PROMPT

    assert ASSISTANT_NAME == "Baer"
    assert f"Your name is {ASSISTANT_NAME}" in SYSTEM_PROMPT
    # The prompt must tell the model that references to the name mean itself, or the
    # model treats "Baer" as a stranger it has never heard of.
    assert "they mean you" in SYSTEM_PROMPT
