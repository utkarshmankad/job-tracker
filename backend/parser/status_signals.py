"""Status signal detection from email content."""

from backend.db.models import ApplicationStatus

# Global keyword lists for status signal detection (used when portal-specific rules don't match)
GLOBAL_STATUS_KEYWORDS: dict[str, list[str]] = {
    ApplicationStatus.RESUME_SHORTLISTED: ["shortlisted", "profile selected", "resume shortlisted", "pleased to inform you", "moved to next"],
    ApplicationStatus.INTERVIEW_SCHEDULED: ["interview scheduled", "would like to schedule", "invite you for an interview", "calendar invite", "interview slot", "interview on"],
    ApplicationStatus.INTERVIEW_IN_PROGRESS: ["following up on your interview", "post-interview", "panel interview today"],
    ApplicationStatus.OFFER_NEGOTIATION: ["discussing compensation", "compensation discussion", "negotiation"],
    ApplicationStatus.OFFER: ["offer letter", "pleased to extend an offer", "formal offer", "job offer", "employment offer", "compensation package"],
    ApplicationStatus.REJECTED: ["regret to inform", "not selected", "moved forward with other candidates", "not shortlisted", "unsuccessful application", "position has been filled", "decided not to proceed"],
}
