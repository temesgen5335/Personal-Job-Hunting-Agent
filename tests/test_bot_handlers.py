"""Runtime coverage for the Telegram handlers in bot/app.py.

`tests/test_bot.py` covers only the pure helpers in `bot/service.py`. The handlers
themselves had NO runtime coverage, which is how a call to an undefined `_llm()` shipped
in the `/apply` fit-check path and crashed it with `NameError`. A static check now guards
that specific class, but a static check is not coverage — it cannot see a handler that
runs and does the wrong thing.

The harness is the `FakePage` pattern applied to python-telegram-bot: fake `Update` and
`Context` objects with the same surface the handlers touch, so every command is exercised
end to end with no bot token, no network, and no event loop of its own (R17).
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from jobagent.bot import app as bot
from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store

OWNER = 4242
STRANGER = 9999


# --- the harness --------------------------------------------------------------

@dataclass
class FakeMessage:
    """Records what the handler said instead of sending it."""

    chat_id: int = OWNER
    replies: list[dict] = field(default_factory=list)
    documents: list[tuple] = field(default_factory=list)

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return self

    async def reply_document(self, *args, **kwargs):
        self.documents.append((args, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.replies.append({"text": text, "edited": True, **kwargs})
        return self

    @property
    def last(self) -> str:
        return self.replies[-1]["text"] if self.replies else ""

    @property
    def all_text(self) -> str:
        return "\n".join(r["text"] for r in self.replies)


@dataclass
class FakeChat:
    id: int = OWNER


@dataclass
class FakeUpdate:
    message: FakeMessage = field(default_factory=FakeMessage)
    chat_id: int = OWNER

    @property
    def effective_chat(self):
        return FakeChat(self.chat_id)

    @property
    def effective_message(self):
        return self.message


@dataclass
class FakeApplication:
    """Handlers read `context.application.bot_data`, not `context.bot_data` — the two
    are different objects in python-telegram-bot and only one is populated by
    `build_application`. Guessing wrong here produced sixteen identical AttributeErrors,
    which is the harness earning its keep before it tested anything."""

    bot_data: dict = field(default_factory=dict)


@dataclass
class FakeContext:
    """Mirrors the slice of ContextTypes.DEFAULT_TYPE the handlers use."""

    args: list[str] = field(default_factory=list)
    application: FakeApplication = field(default_factory=FakeApplication)
    user_data: dict = field(default_factory=dict)
    chat_data: dict = field(default_factory=dict)

    @property
    def bot_data(self) -> dict:
        # A couple of handlers use the short form; both must reach the same dict.
        return self.application.bot_data


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def wired(tmp_path):
    """A store with two scored jobs, plus the bot_data the handlers read."""
    db = str(tmp_path / "bot.db")
    store = Store(db)
    store.init_schema()
    for title, company, score in (("AI Engineer", "Acme", 0.91),
                                  ("Backend Engineer", "Globex", 0.72)):
        jid = store.upsert_job(JobPosting(source=Source.remoteok, title=title,
                                          company=company, is_remote=True,
                                          location="Remote"))
        store.upsert_match(Match(job_id=jid, score=score, rationale="skills: Python"))
    store.close()

    # Keys mirror `build_application` exactly — read off bot/app.py rather than
    # remembered, because a stub that drifts from the real wiring tests nothing.
    from jobagent.bot.service import MatchFilter
    from jobagent.config import Settings
    from jobagent.preferences import Profile

    settings = Settings(_env_file=None, JOBAGENT_DB_PATH=db)
    profile = Profile(name="Tester", target_roles=["Engineer"], core_skills=["Python"])
    ctx = FakeContext(application=FakeApplication(bot_data={
        "settings": settings, "owner_id": OWNER, "profile": profile,
        "llm": None, "cv_master": "CV TEXT",
        "filter": MatchFilter.from_profile(profile),
    }))
    return ctx, db


# --- the owner gate -----------------------------------------------------------

@pytest.mark.parametrize("handler", ["menu", "jobs", "status", "apply_cmd", "ask_cmd"])
def test_every_command_refuses_a_stranger(wired, handler):
    """The bot is single-user and fails closed. This is the property most worth a
    runtime test: a handler that forgot `_guard` would answer anyone who found it."""
    ctx, _ = wired
    update = FakeUpdate(chat_id=STRANGER)
    run(getattr(bot, handler)(update, ctx))
    assert "private bot" in update.message.last.lower()


def test_the_gate_fails_closed_with_no_owner_configured(wired):
    """No owner set must deny everyone, not allow everyone — the direction of this
    default is the whole point."""
    ctx, _ = wired
    ctx.application.bot_data["owner_id"] = None
    update = FakeUpdate(chat_id=OWNER)
    run(bot.status(update, ctx))
    assert "private bot" in update.message.last.lower()


# --- commands actually run ----------------------------------------------------

def test_jobs_lists_matches(wired):
    ctx, _ = wired
    update = FakeUpdate()
    run(bot.jobs(update, ctx))
    assert "AI Engineer" in update.message.all_text


def test_jobs_respects_a_count_argument(wired):
    ctx, _ = wired
    ctx.args = ["1"]
    update = FakeUpdate()
    run(bot.jobs(update, ctx))
    text = update.message.all_text
    assert "AI Engineer" in text and "Backend Engineer" not in text


def test_jobs_survives_a_nonsense_count(wired):
    """`/jobs banana` must not raise — an exception here kills the whole handler and
    the user sees nothing at all."""
    ctx, _ = wired
    ctx.args = ["banana"]
    update = FakeUpdate()
    run(bot.jobs(update, ctx))
    assert update.message.replies, "the handler answered nothing"


def test_status_reports_real_store_numbers(wired):
    ctx, _ = wired
    update = FakeUpdate()
    run(bot.status(update, ctx))
    text = update.message.last
    assert "Total jobs: 2" in text
    assert "None" not in text, "a None in bot output is a guessed store key (R32)"


def test_menu_renders_with_a_keyboard(wired):
    ctx, _ = wired
    update = FakeUpdate()
    run(bot.menu(update, ctx))
    assert update.message.replies[-1].get("reply_markup") is not None


# --- /apply: the path that shipped a NameError --------------------------------

def test_apply_without_a_rank_explains_itself(wired):
    ctx, _ = wired
    update = FakeUpdate()
    run(bot.apply_cmd(update, ctx))
    assert "usage" in update.message.last.lower()


def test_apply_with_a_non_numeric_rank_is_handled(wired):
    ctx, _ = wired
    ctx.args = ["three"]
    update = FakeUpdate()
    run(bot.apply_cmd(update, ctx))
    assert "number" in update.message.last.lower()


def test_apply_with_an_out_of_range_rank_says_so(wired):
    """Reaches `_start_apply` for real. This is the code path where an undefined
    `_llm()` once shipped: it is only entered with a plausible rank, so nothing before
    this test ever executed it."""
    ctx, _ = wired
    ctx.args = ["99"]
    update = FakeUpdate()
    run(bot.apply_cmd(update, ctx))
    assert update.message.replies, "the handler answered nothing"
    assert "None" not in update.message.all_text


def test_ask_without_a_question_prompts_for_one(wired):
    ctx, _ = wired
    update = FakeUpdate()
    run(bot.ask_cmd(update, ctx))
    text = update.message.last.lower()
    assert "usage" in text or "ask" in text


# --- no handler renders a None into user-visible text -------------------------

def test_no_command_emits_none_into_a_message(wired):
    """The R32 class, applied to the bot. A model is not the only consumer that reads
    `None` as a fact — a human does too, and the bot is the primary interface."""
    ctx, _ = wired
    leaks = {}
    for name, args in (("menu", []), ("jobs", []), ("status", []), ("apply_cmd", ["99"])):
        ctx.args = list(args)
        update = FakeUpdate()
        run(getattr(bot, name)(update, ctx))
        if "None" in update.message.all_text:
            leaks[name] = update.message.all_text[:160]
    assert leaks == {}, f"handlers rendered None into user-visible text: {leaks}"


# --- LLM usage accounting -----------------------------------------------------

def test_usage_counts_calls_failures_and_estimates_tokens():
    """Estimates, and named so everywhere. A number presented as billed usage when it
    is a guess gets trusted, which is worse than having no number."""
    from jobagent.llm_client import LLMUsage

    usage = LLMUsage()
    usage.record("groq", "p" * 400, "c" * 200)
    usage.record("groq", "p" * 400, "c" * 200)
    usage.record_failure("gemini")

    data = usage.as_dict()
    assert data["calls"] == 2 and data["failures"] == 1
    assert data["estimated_tokens"] == 300          # 1200 chars / 4
    assert "estimated" in "".join(k for k in data if "token" in k)
    assert data["by_provider"]["gemini"]["failures"] == 1


def test_a_failing_provider_is_recorded_even_though_it_returned_nothing():
    """A chain whose first backend is dead is otherwise invisible — the answer still
    arrives from the next one. That is exactly how two dead model slugs went unnoticed
    for weeks."""
    from jobagent.llm_client import MultiLLM

    class Dead:
        name = "dead"

        def generate(self, system, user):
            raise RuntimeError("404 model_not_found")

    class Alive:
        name = "alive"

        def generate(self, system, user):
            return "answer"

    llm = MultiLLM([Dead(), Alive()])
    assert llm.complete("s", "u") == "answer"
    data = llm.usage.as_dict()
    assert data["by_provider"]["dead"]["failures"] == 1
    assert data["by_provider"]["alive"]["calls"] == 1
