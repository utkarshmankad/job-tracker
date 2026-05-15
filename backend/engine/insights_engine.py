"""Insights and analytics computation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import structlog

from backend.config import INTERVIEW_RATE_GREEN_THRESHOLD, MIN_APPLICATIONS_FOR_INSIGHTS
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, ApplicationStatus

log = structlog.get_logger(__name__)

_SHORTLISTED_STAGES: frozenset[ApplicationStatus] = frozenset({
    ApplicationStatus.RESUME_SHORTLISTED,
    ApplicationStatus.INTERVIEW_SCHEDULED,
    ApplicationStatus.INTERVIEW_IN_PROGRESS,
    ApplicationStatus.OFFER_NEGOTIATION,
    ApplicationStatus.OFFER,
    ApplicationStatus.JOINED,
})

_INTERVIEW_STAGES: frozenset[ApplicationStatus] = frozenset({
    ApplicationStatus.INTERVIEW_SCHEDULED,
    ApplicationStatus.INTERVIEW_IN_PROGRESS,
    ApplicationStatus.OFFER_NEGOTIATION,
    ApplicationStatus.OFFER,
    ApplicationStatus.JOINED,
})

_OFFER_STAGES: frozenset[ApplicationStatus] = frozenset({
    ApplicationStatus.OFFER,
    ApplicationStatus.JOINED,
})


@dataclass
class ChannelStat:
    source: str
    total: int
    shortlisted: int
    interviewed: int
    offered: int

    def interview_rate(self) -> float:
        return self.interviewed / self.total if self.total > 0 else 0.0

    def offer_rate(self) -> float:
        return self.offered / self.total if self.total > 0 else 0.0


@dataclass
class ChannelInsight:
    source: str
    flag: str  # "green" | "red" | "neutral"
    message: str


@dataclass
class InsightReport:
    funnel: dict[str, int]
    channels: list[ChannelStat]
    insights: list[ChannelInsight]
    total_applications: int
    insufficient_data: bool
    generated_at: datetime


class InsightsEngine:
    def __init__(self, db: DataStore) -> None:
        self._db = db

    def generate_report(self) -> InsightReport:
        apps = self._fetch_active_apps()
        funnel = self._funnel_counts(apps)
        total = sum(funnel.values())

        if total < MIN_APPLICATIONS_FOR_INSIGHTS:
            return InsightReport(
                funnel=funnel,
                channels=[],
                insights=[],
                total_applications=total,
                insufficient_data=True,
                generated_at=datetime.utcnow(),
            )

        channels = self._channel_stats(apps)
        insights = self._flag_channels(channels)

        return InsightReport(
            funnel=funnel,
            channels=channels,
            insights=insights,
            total_applications=total,
            insufficient_data=False,
            generated_at=datetime.utcnow(),
        )

    def _fetch_active_apps(self) -> list[Application]:
        apps, _ = self._db.get_applications(ApplicationFilter(page_size=10_000))
        return [a for a in apps if not a.is_false_positive]

    def _funnel_counts(self, apps: list[Application]) -> dict[str, int]:
        counts: dict[str, int] = {s.value: 0 for s in ApplicationStatus}
        for app in apps:
            counts[app.current_status.value] += 1
        return counts

    def _channel_stats(self, apps: list[Application]) -> list[ChannelStat]:
        groups: dict[str, list[Application]] = {}
        for app in apps:
            groups.setdefault(app.source_portal, []).append(app)

        stats: list[ChannelStat] = []
        for source, group in groups.items():
            stats.append(ChannelStat(
                source=source,
                total=len(group),
                shortlisted=sum(1 for a in group if a.current_status in _SHORTLISTED_STAGES),
                interviewed=sum(1 for a in group if a.current_status in _INTERVIEW_STAGES),
                offered=sum(1 for a in group if a.current_status in _OFFER_STAGES),
            ))
        return stats

    # ------------------------------------------------------------------ #
    # Flow / Sankey data                                                   #
    # ------------------------------------------------------------------ #

    _SHORTLISTED_VALUES: frozenset[str] = frozenset(s.value for s in _SHORTLISTED_STAGES)
    _INTERVIEWED_VALUES: frozenset[str] = frozenset(s.value for s in _INTERVIEW_STAGES)
    _OFFERED_VALUES: frozenset[str] = frozenset(s.value for s in _OFFER_STAGES)
    _TERMINAL_VALUES: frozenset[str] = frozenset({
        ApplicationStatus.REJECTED.value,
        ApplicationStatus.WITHDRAWN.value,
    })

    def flow_data(self) -> dict:
        apps = self._fetch_active_apps()
        total = len(apps)

        if total == 0:
            return {
                "nodes": [], "links": [], "outcomes": [],
                "kpis": {"total": 0, "interview_rate": 0.0, "offer_rate": 0.0, "active": 0},
                "weekly_activity": [],
                "insufficient_data": True,
            }

        # Build set of stages each app passed through using StatusHistory (filtered to active apps)
        app_ids = {app.id for app in apps if app.id is not None}
        all_history = self._db.get_status_history_for_apps(app_ids)
        stages_by_app: dict[int, set[str]] = {}
        for h in all_history:
            if h.application_id is not None:
                stages_by_app.setdefault(h.application_id, set()).add(h.to_status)

        # Counters per broad stage
        rejected_direct = active_applied = 0
        rejected_post_short = active_short = 0
        rejected_post_int = active_int = 0
        offered = 0

        for app in apps:
            stages = stages_by_app.get(app.id, set()) | {app.current_status.value}
            current = app.current_status.value
            is_terminal = current in self._TERMINAL_VALUES

            ever_offered = bool(stages & self._OFFERED_VALUES)
            ever_interviewed = bool(stages & self._INTERVIEWED_VALUES)
            ever_shortlisted = bool(stages & self._SHORTLISTED_VALUES)

            if ever_offered:
                offered += 1

            if ever_interviewed:
                if is_terminal:
                    rejected_post_int += 1
                elif not ever_offered:
                    active_int += 1
            elif ever_shortlisted:
                if is_terminal:
                    rejected_post_short += 1
                else:
                    active_short += 1
            else:
                if is_terminal:
                    rejected_direct += 1
                else:
                    active_applied += 1

        shortlisted_total = (
            offered + rejected_post_int + active_int + rejected_post_short + active_short
        )
        interview_total = offered + rejected_post_int + active_int
        total_active = active_applied + active_short + active_int
        total_rejected = rejected_direct + rejected_post_short + rejected_post_int

        nodes = [
            {"id": "Applied", "count": total},
            {"id": "Shortlisted", "count": shortlisted_total},
            {"id": "Interview", "count": interview_total},
            {"id": "Offer / Joined", "count": offered},
            {"id": "Rejected", "count": total_rejected},
            {"id": "Active", "count": total_active},
        ]

        links: list[dict] = []
        if shortlisted_total:
            links.append({"source": "Applied", "target": "Shortlisted", "value": shortlisted_total})
        if rejected_direct:
            links.append({"source": "Applied", "target": "Rejected", "value": rejected_direct})
        if active_applied:
            links.append({"source": "Applied", "target": "Active", "value": active_applied})
        if interview_total:
            links.append({"source": "Shortlisted", "target": "Interview", "value": interview_total})
        if rejected_post_short:
            links.append({"source": "Shortlisted", "target": "Rejected", "value": rejected_post_short})
        if active_short:
            links.append({"source": "Shortlisted", "target": "Active", "value": active_short})
        if offered:
            links.append({"source": "Interview", "target": "Offer / Joined", "value": offered})
        if rejected_post_int:
            links.append({"source": "Interview", "target": "Rejected", "value": rejected_post_int})
        if active_int:
            links.append({"source": "Interview", "target": "Active", "value": active_int})

        outcomes = [
            {"name": "Active", "value": total_active, "color": "#3b82f6"},
            {"name": "Offer / Joined", "value": offered, "color": "#22c55e"},
            {"name": "Rejected", "value": total_rejected, "color": "#ef4444"},
        ]

        return {
            "nodes": nodes,
            "links": links,
            "outcomes": outcomes,
            "kpis": {
                "total": total,
                "interview_rate": round(interview_total / total, 4) if total else 0.0,
                "offer_rate": round(offered / total, 4) if total else 0.0,
                "active": total_active,
            },
            "weekly_activity": self._weekly_activity(apps),
            "insufficient_data": total < MIN_APPLICATIONS_FOR_INSIGHTS,
        }

    def _weekly_activity(self, apps: list[Application]) -> list[dict]:
        today = date.today()
        # Start from Monday of the week 11 weeks ago
        start_monday = today - timedelta(weeks=11, days=today.weekday())
        weeks = [start_monday + timedelta(weeks=i) for i in range(12)]

        counts: dict[date, int] = {w: 0 for w in weeks}
        for app in apps:
            app_date = (
                app.applied_date.date()
                if isinstance(app.applied_date, datetime)
                else app.applied_date
            )
            for i, week_start in enumerate(weeks):
                week_end = week_start + timedelta(days=7)
                if week_start <= app_date < week_end:
                    counts[week_start] += 1
                    break

        return [{"week": w.isoformat(), "count": counts[w]} for w in weeks]

    # ------------------------------------------------------------------ #

    def _flag_channels(self, stats: list[ChannelStat]) -> list[ChannelInsight]:
        insights: list[ChannelInsight] = []
        for stat in stats:
            rate = stat.interview_rate()
            if rate > INTERVIEW_RATE_GREEN_THRESHOLD:
                flag = "green"
                message = (
                    f"{stat.source} is converting at {rate:.0%} interview rate"
                    f" — prioritize this channel."
                )
            elif stat.total >= 10 and rate == 0.0:
                flag = "red"
                message = (
                    f"No responses from {stat.source} after {stat.total} applications"
                    f" — consider reducing volume here."
                )
            else:
                flag = "neutral"
                message = f"{stat.source} has a {rate:.0%} interview rate."
            insights.append(ChannelInsight(source=stat.source, flag=flag, message=message))
        return insights
