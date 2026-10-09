"""Adapters for job sites' own application-history pages.

REAL-SESSION VALIDATION (2026-10-09, read-only, user's own signed-in browser):

- indeed: history page confirmed; selectors rewritten from the live structure (stable
  ``data-testid`` and ARIA hooks, not generated class names); all rows read and
  matched the site's own count in two identical runs → ``LIVE_VERIFIED`` set.
- linkedin: the history moved to a new "Job tracker" (``/jobs-tracker/?stage=applied``)
  with generated class names, no row markers and job links outside their rows. No stable
  selectors exist yet → ``SUPPORTED = False`` (the old page now redirects; the adapter
  would have stopped with ``unexpected_page``, never a false empty result).
- naukri: the real history page is ``/myapply/historypage`` (the old guess showed a
  registration page for an unknown route — not a sign-out). Its cards carry title,
  company and a status chip, but no job link or item ID, no applied date (the chip's time
  is the last status change), only ~7 of the site's total are rendered (the rest load by
  scrolling an inner panel), and "applies on external site" entries are not proof of
  submission → ``SUPPORTED = False`` until an adapter handles identity and dates safely.
- instahyre: there is no application-history page; "Activity" lists recruiters who
  viewed the résumé (with their names) — not proof of an application, and personal data
  the collector must not take → ``SUPPORTED = False``.
- careernet: the candidate platform is a separate site (mycareernet); the guessed
  history URL does not exist and the profile was signed out there → ``SUPPORTED = False``
  until its history page is located.

READINESS: Indeed is the only source ready for controlled collection. The unsupported
adapters keep their best structural reading of each site only as a starting point for
rework; ``adapter_for`` refuses them. ``SUPPORTED``, ``UNSUPPORTED_REASON`` and
``LIVE_VERIFIED`` here must agree with backend/collection/readiness.py and the docs'
readiness tables (tests/unit/test_source_readiness_consistency.py). Any adapter without
``LIVE_VERIFIED`` (for example an employer definition) yields ``extraction="unverified"``,
which never creates an application automatically — it links only on strong identifiers
or goes to review (docs/phase-3-source-collection.md §5). When a live page does not
match, an adapter stops with ``selector_drift`` or ``unexpected_page`` — it never reports
an empty success.

Verifying or reworking an adapter is the generic supervised process in
docs/collector-operations.md §5.
"""

from __future__ import annotations

import re

from collector.adapters.base import Adapter, SelectorSet, StateMarkers


class LinkedInAdapter(Adapter):
    """LinkedIn "My Jobs → Applied" (Easy Apply and external applications tracked there)."""

    SOURCE_KEY = "linkedin"
    VERSION = "0.2.0"
    HISTORY_URL = "https://www.linkedin.com/jobs-tracker/?stage=applied"
    SUPPORTED = False
    UNSUPPORTED_REASON = "The new Job Tracker layout lacks safe stable row boundaries."
    ALLOWED_HOSTS = ("linkedin.com",)
    PAGINATION = "next"
    NEXT_SELECTOR = "button.artdeco-pagination__button--next"
    ITEM_ID_FROM_URL = re.compile(r"/jobs/view/(\d{5,})")
    SELECTORS = (
        SelectorSet(
            "primary",
            row="li.reusable-search__result-container",
            company=".entity-result__primary-subtitle",
            role=".entity-result__title-text a",
            status=".entity-result__simple-insight-text",
            applied=".entity-result__simple-insight-text",
            link=".entity-result__title-text a",
        ),
        SelectorSet(
            "fallback",
            row="div.job-card-container",
            company=".job-card-container__company-name",
            role=".job-card-list__title",
            status=".job-card-container__footer-item",
            applied=".job-card-container__footer-item",
            link="a.job-card-list__title",
        ),
    )
    MARKERS = StateMarkers(
        signed_out=("form.login__form", "#session_key", "a.main__sign-in-link"),
        signed_out_urls=("/login", "/uas/login", "/authwall", "/signup"),
        consent=("#artdeco-global-alert-container .artdeco-global-alert--consent",),
        empty=(".artdeco-empty-state",),
        list_container=(".workflow-results-container", "ul.reusable-search__entity-result-list"),
    )
    STATUS_MAP = {
        "applied": "applied",
        "application viewed": "viewed",
        "resume downloaded": "in_review",
        "no longer accepting applications": "closed",
        "application submitted": "applied",
    }


class NaukriAdapter(Adapter):
    """Naukri "Applies" history."""

    SOURCE_KEY = "naukri"
    VERSION = "0.2.0"
    HISTORY_URL = "https://www.naukri.com/myapply/historypage"
    SUPPORTED = False
    UNSUPPORTED_REASON = (
        "Unsupported until item identity, applied dates and complete inner-scroll "
        "collection can be established."
    )
    ALLOWED_HOSTS = ("naukri.com",)
    PAGINATION = "load_more"
    LOAD_MORE_SELECTOR = "button.apply-history-load-more"
    ITEM_ID_FROM_URL = re.compile(r"-(\d{10,})(?:\?|$)")
    SELECTORS = (
        SelectorSet(
            "primary",
            row="div.apply-history-card",
            company=".apply-history-card__company",
            role=".apply-history-card__title",
            status=".apply-history-card__status",
            applied=".apply-history-card__date",
            link="a.apply-history-card__title",
            item_id_attr="data-job-id",
        ),
        SelectorSet(
            "fallback",
            row="article.jobTuple",
            company=".comp-name",
            role=".title",
            status=".status",
            applied=".applied-date",
            link="a.title",
        ),
    )
    MARKERS = StateMarkers(
        # Not a login *link*: Naukri shows a registration page with one for unknown routes,
        # which must surface as unexpected_page, not as "signed out".
        signed_out=("form#loginForm", "#usernameField"),
        signed_out_urls=("/nlogin/login", "/login"),
        empty=(".apply-history-empty",),
        list_container=(".apply-history-list", "section.jobTupleList"),
    )
    STATUS_MAP = {
        "applied": "applied",
        "application sent": "applied",
        "recruiter viewed": "viewed",
        "application viewed": "viewed",
        "shortlisted": "shortlisted",
        "not shortlisted": "rejected",
        "rejected": "rejected",
        "interview": "interview",
        "job closed": "closed",
    }


class IndeedAdapter(Adapter):
    """Indeed "My jobs → Applied". Selectors from the live page (2026-10-09): cards are
    list items carrying ``data-testid="jobStatusDateShort"``; the title link sits in the
    card header's ARIA heading; the site's own "<n> Applied" tab label gives the count."""

    SOURCE_KEY = "indeed"
    VERSION = "0.2.0"
    # Verified 2026-10-09 in the user's signed-in session: every selector read all rows,
    # the row count matched the site's own "<n> Applied" count, values matched the page,
    # and two runs produced identical identities. The selectors were evaluated by the
    # browser's CSS engine (the dedicated Playwright profile could not stay signed in);
    # bs4/soupsieve support the same selector syntax and is covered by the fixtures.
    LIVE_VERIFIED = "2026-10-09"
    HISTORY_URL = "https://myjobs.indeed.com/applied"
    ALLOWED_HOSTS = ("indeed.com",)
    PAGINATION = "none"
    ITEM_ID_FROM_URL = re.compile(r"[?&]jk=([0-9a-f]{12,})")
    ROLE_NOISE = (r"\s*job description opens in a new window\s*$",)
    EXPECTED_COUNT_SELECTOR = "[data-testid='APPLIED']"
    EXPECTED_COUNT_PATTERN = re.compile(r"(\d+)\s+Applied", re.IGNORECASE)
    SELECTORS = (
        SelectorSet(
            "primary",
            row="li:has([data-testid='jobStatusDateShort'])",
            company="header [role='heading'] + div > span:first-child",
            role="header [role='heading'] a",
            status="header > div:first-child span",
            applied="[data-testid='jobStatusDateShort']",
            link="header [role='heading'] a",
        ),
    )
    MARKERS = StateMarkers(
        signed_out=("form#loginform", "[data-testid='auth-page']"),
        signed_out_urls=("secure.indeed.com/auth", "/account/login"),
        list_container=("[data-testid='jobStatusDateShort']",),
    )
    STATUS_MAP = {
        "applied": "applied",
        "application viewed": "viewed",
        "application reviewed": "in_review",
        "not selected": "rejected",
        "not selected by employer": "rejected",
        "interviewing": "interview",
        "offer received": "offer",
        "hired": "offer",
        "no longer available": "closed",
    }


class InstahyreAdapter(Adapter):
    """Instahyre candidate application activity."""

    SOURCE_KEY = "instahyre"
    VERSION = "0.2.0"
    SUPPORTED = False
    UNSUPPORTED_REASON = "Instahyre has no application-history page."
    HISTORY_URL = "https://www.instahyre.com/candidate/applications/"
    ALLOWED_HOSTS = ("instahyre.com",)
    PAGINATION = "scroll"
    LOAD_MORE_SELECTOR = "div.application-list-loader"
    SELECTORS = (
        SelectorSet(
            "primary",
            row="div.application-card",
            company=".application-card__company",
            role=".application-card__designation",
            status=".application-card__status",
            applied=".application-card__applied-on",
            link="a.application-card__job-link",
            item_id_attr="data-application-id",
        ),
    )
    MARKERS = StateMarkers(
        signed_out=("form#login-form", "input[name='email'][type='email']"),
        signed_out_urls=("/login", "/signup"),
        empty=(".applications-empty",),
        list_container=(".applications-list",),
    )
    STATUS_MAP = {
        "applied": "applied",
        "viewed": "viewed",
        "under review": "in_review",
        "shortlisted": "shortlisted",
        "interview": "interview",
        "rejected": "rejected",
        "not interested": "rejected",
        "offer": "offer",
        "withdrawn": "withdrawn",
    }


class CareerNetAdapter(Adapter):
    """CareerNet candidate applications (table layout)."""

    SOURCE_KEY = "careernet"
    VERSION = "0.2.0"
    SUPPORTED = False
    UNSUPPORTED_REASON = "CareerNet's candidate history page has not been located."
    HISTORY_URL = "https://www.careernet.in/candidate/applications"
    ALLOWED_HOSTS = ("careernet.in",)
    PAGINATION = "next"
    NEXT_SELECTOR = "a.pagination-next"
    SELECTORS = (
        SelectorSet(
            "primary",
            row="table.applications-table tbody tr.application-row",
            company="td.col-company",
            role="td.col-position",
            status="td.col-status",
            applied="td.col-applied",
            link="td.col-position a",
            item_id_attr="data-application-ref",
        ),
    )
    MARKERS = StateMarkers(
        signed_out=("form.candidate-login",),
        signed_out_urls=("/candidate/login", "/login"),
        empty=(".applications-empty",),
        list_container=("table.applications-table",),
    )
    STATUS_MAP = {
        "applied": "applied",
        "submitted": "applied",
        "profile viewed": "viewed",
        "under review": "in_review",
        "shortlisted": "shortlisted",
        "interview": "interview",
        "not shortlisted": "rejected",
        "rejected": "rejected",
        "offer": "offer",
        "withdrawn": "withdrawn",
        "position closed": "closed",
    }


SITE_ADAPTERS: tuple[type[Adapter], ...] = (
    LinkedInAdapter,
    NaukriAdapter,
    IndeedAdapter,
    InstahyreAdapter,
    CareerNetAdapter,
)
