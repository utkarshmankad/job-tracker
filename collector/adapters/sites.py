"""Adapters for job sites' own application-history pages.

STATUS OF EVERY ADAPTER IN THIS FILE: fixture-tested only. The history URLs and selectors
are a best structural reading of each site and have NOT been confirmed against the live
sites (``LIVE_VERIFIED = None``). Consequences, by design:

- observations are marked ``extraction="unverified"``, and the tracker never creates an
  application from them automatically — they link only on strong identifiers or go to
  review (docs/phase-3-source-collection.md §5);
- when a live page does not match, the adapter stops with ``selector_drift`` or
  ``unexpected_page`` and (if enabled) saves an encrypted snapshot for repair — it never
  reports an empty success.

To verify an adapter: run ``scripts/collect.py run --source <key> --dry-run`` against your
signed-in profile, compare the saved dry-run file with the site, fix selectors, refresh the
sanitized fixtures and set ``LIVE_VERIFIED`` to the date (docs/collector-operations.md).
"""

from __future__ import annotations

import re

from collector.adapters.base import Adapter, SelectorSet, StateMarkers


class LinkedInAdapter(Adapter):
    """LinkedIn "My Jobs → Applied" (Easy Apply and external applications tracked there)."""

    SOURCE_KEY = "linkedin"
    VERSION = "0.1.0"
    HISTORY_URL = "https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED"
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
    VERSION = "0.1.0"
    HISTORY_URL = "https://www.naukri.com/mnjuser/applies"
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
    """Indeed "My jobs → Applied"."""

    SOURCE_KEY = "indeed"
    VERSION = "0.1.0"
    HISTORY_URL = "https://myjobs.indeed.com/applied"
    ALLOWED_HOSTS = ("indeed.com",)
    PAGINATION = "load_more"
    LOAD_MORE_SELECTOR = "button[data-testid='myjobs-load-more']"
    ITEM_ID_FROM_URL = re.compile(r"[?&]jk=([0-9a-f]{12,})")
    SELECTORS = (
        SelectorSet(
            "primary",
            row="div[data-testid='myjobs-card']",
            company="[data-testid='myjobs-company']",
            role="[data-testid='myjobs-title']",
            status="[data-testid='myjobs-status']",
            applied="[data-testid='myjobs-applied-date']",
            link="a[data-testid='myjobs-title']",
        ),
        SelectorSet(
            "fallback",
            row="li.atw-JobCard",
            company=".atw-JobInfo-companyName",
            role=".atw-JobInfo-jobTitle",
            status=".atw-JobCard-status",
            applied=".atw-JobInfo-appliedDate",
            link="a.atw-JobInfo-jobTitle",
        ),
    )
    MARKERS = StateMarkers(
        signed_out=("form#loginform", "[data-testid='auth-page']"),
        signed_out_urls=("secure.indeed.com/auth", "/account/login"),
        empty=("[data-testid='myjobs-empty']",),
        list_container=("[data-testid='myjobs-list']", "ul.atw-JobList"),
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
    VERSION = "0.1.0"
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
    VERSION = "0.1.0"
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
