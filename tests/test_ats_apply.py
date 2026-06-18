"""Phase 4 tests: ATS form-fill engine. FakePage stands in for Playwright — no
browser, no network. Asserts R2 (no unrequested submit) and R3 (CAPTCHA blocks submit)."""

from jobagent.apply.ats import ApplicantInfo, apply_target, detect_platform, execute, field_plan
from jobagent.preferences import Profile


class FakePage:
    def __init__(self, present: set[str], html: str = ""):
        self.present = present
        self._html = html
        self.filled: dict[str, str] = {}
        self.uploaded: dict[str, str] = {}
        self.clicked: list[str] = []
        self.screenshots: list[str] = []

    def query_selector(self, sel):
        return object() if sel in self.present else None

    def fill(self, sel, val):
        self.filled[sel] = val

    def set_input_files(self, sel, path):
        self.uploaded[sel] = path

    def click(self, sel):
        self.clicked.append(sel)

    def screenshot(self, path=None):
        self.screenshots.append(path)

    def content(self):
        return self._html


APPLICANT = ApplicantInfo(
    full_name="Temesgen Gebreabzgi", first_name="Temesgen", last_name="Gebreabzgi",
    email="me@example.com", phone="+251900000000",
    linkedin="https://linkedin.com/in/x", github="", cv_path="/tmp/cv.pdf",
)


def test_detect_platform():
    assert detect_platform("https://boards.greenhouse.io/stripe/jobs/1") == "greenhouse"
    assert detect_platform("https://jobs.lever.co/netflix/abc") == "lever"
    assert detect_platform("https://jobs.ashbyhq.com/ramp/x") == "ashby"
    assert detect_platform("https://indeed.com/viewjob?jk=1") is None


def test_apply_target_uses_source_for_custom_domain():
    # Real case: Greenhouse-sourced job whose apply_url is a custom careers domain.
    job = {"source": "greenhouse", "company": "instacart", "source_job_id": "7974573",
           "apply_url": "https://instacart.careers/job/?gh_jid=7974573", "url": ""}
    platform, url = apply_target(job)
    assert platform == "greenhouse"
    assert url == "https://job-boards.greenhouse.io/instacart/jobs/7974573"  # canonical form, not the redirect


def test_apply_target_passes_through_native_urls():
    gh = {"source": "greenhouse", "company": "stripe", "source_job_id": "9",
          "apply_url": "https://boards.greenhouse.io/stripe/jobs/9"}
    assert apply_target(gh) == ("greenhouse", "https://boards.greenhouse.io/stripe/jobs/9")
    lever = {"source": "lever", "apply_url": "https://jobs.lever.co/x/abc/apply"}
    assert apply_target(lever) == ("lever", "https://jobs.lever.co/x/abc/apply")
    ashby = {"source": "ashby", "apply_url": "https://jobs.ashbyhq.com/ramp/z/application"}
    assert apply_target(ashby) == ("ashby", "https://jobs.ashbyhq.com/ramp/z/application")


def test_apply_target_none_for_unsupported():
    assert apply_target({"source": "remoteok", "apply_url": "https://acme.io/jobs/1"})[0] is None


def test_applicant_from_profile_splits_name():
    a = ApplicantInfo.from_profile(Profile(name="Temesgen Gebreabzgi", email="e@x.com",
                                           phone="123", links={"github": "gh", "linkedin": "li"}))
    assert a.first_name == "Temesgen" and a.last_name == "Gebreabzgi"
    assert a.github == "gh" and a.linkedin == "li"


def test_execute_fills_present_fields_and_reports_missing(tmp_path):
    plan = field_plan("greenhouse", APPLICANT)
    # Phone field is absent on this form → should land in `missing`.
    page = FakePage(present={"#first_name", "#last_name", "#email", "input[type=file]"})
    res = execute(page, "greenhouse", plan, str(tmp_path / "s.png"), submit=False)

    assert "Email" in res.filled
    assert page.filled["#email"] == "me@example.com"
    assert page.uploaded["input[type=file]"] == "/tmp/cv.pdf"   # resume upload
    assert "Phone" in res.missing                                # not present on page
    assert page.screenshots == [str(tmp_path / "s.png")]         # screenshot always taken
    assert res.submitted is False                                # R2: never without submit=True
    assert page.clicked == []


def test_empty_value_fields_skipped_not_missing(tmp_path):
    # GitHub is empty for APPLICANT → action skipped entirely, not reported missing.
    plan = field_plan("lever", APPLICANT)
    page = FakePage(present={"input[name=name]", "input[name=email]", "input[name=phone]",
                             "input[name='urls[LinkedIn]']", "input[name=resume]"})
    res = execute(page, "lever", plan, str(tmp_path / "s.png"))
    assert "GitHub" not in res.missing
    assert "GitHub" not in res.filled
    assert "LinkedIn" in res.filled


def test_submit_clicks_only_when_requested(tmp_path):
    plan = field_plan("lever", APPLICANT)
    present = {"input[name=name]", "input[name=email]", "button[type=submit]"}
    page = FakePage(present=present)
    res = execute(page, "lever", plan, str(tmp_path / "s.png"), submit=True)
    assert res.submitted is True
    assert "button[type=submit]" in page.clicked


def test_captcha_blocks_submit_R3(tmp_path):
    plan = field_plan("greenhouse", APPLICANT)
    # A *rendered* recaptcha element is present (not just a JS string).
    page = FakePage(present={"#email", "button[type=submit]", ".g-recaptcha"})
    res = execute(page, "greenhouse", plan, str(tmp_path / "s.png"), submit=True)
    assert res.captcha_detected is True
    assert res.submitted is False        # R3: never auto-submit through a CAPTCHA
    assert page.clicked == []


def test_no_false_captcha_without_rendered_element(tmp_path):
    # No challenge element present → not flagged, even though real bundles ship the libs.
    page = FakePage(present={"#email", "button[type=submit]"})
    res = execute(page, "greenhouse", field_plan("greenhouse", APPLICANT),
                  str(tmp_path / "s.png"), submit=True)
    assert res.captcha_detected is False


def test_summary_reflects_state(tmp_path):
    page = FakePage(present={"#email"})
    res = execute(page, "greenhouse", field_plan("greenhouse", APPLICANT), str(tmp_path / "s.png"))
    assert "NOT submitted" in res.summary()
