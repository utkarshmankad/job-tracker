"""Insights and analytics computation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
        funnel = self._funnel_counts()
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

        channels = self._channel_stats()
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

    def _funnel_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {s.value: 0 for s in ApplicationStatus}
        for app in self._fetch_active_apps():
            counts[app.current_status.value] += 1
        return counts

    def _channel_stats(self) -> list[ChannelStat]:
        groups: dict[str, list[Application]] = {}
        for app in self._fetch_active_apps():
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
