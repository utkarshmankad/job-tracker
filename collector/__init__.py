"""Local, read-only application-history collector (docs/phase-3-source-collection.md).

Runs on the user's own computer next to a browser profile they are signed in to, visits
job sites' application-history pages, and sends minimal normalized observations to Job
Tracker with a scoped credential. It never submits, withdraws, messages, edits profiles,
accepts anything, or bypasses a CAPTCHA or access challenge.
"""

COLLECTOR_VERSION = "0.1.0"
