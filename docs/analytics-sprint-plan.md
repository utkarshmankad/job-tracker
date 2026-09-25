# Analytics improvement sprint plan

The backlog is ordered by expected impact on weekly job-search decisions, then by data dependency.

## Sprint 1 — Make the dashboard trustworthy and actionable

Goal: distinguish current effort from imported history and measure conversion only after applications have had time to receive a response.

- [x] Add 7, 28, and 90-day Search Pulse views.
- [x] Separate application date from tracker capture date in the activity chart.
- [x] Flag applications captured more than seven days after they were submitted.
- [x] Add 14-day matured-cohort response, shortlist, interview, and offer metrics.
- [x] Replace the primary Sankey with a compact stage funnel and conditional conversion rates.
- [x] Retain lifetime metrics as secondary context.
- [x] Cover the new calculations and endpoint with automated tests.

Success criteria:

- A user can see their recent application rate without historical backfills distorting it.
- Recent applications are not counted as conversion failures during the response window.
- Applied → shortlisted → interview → offer leakage is understandable at a glance.

## Sprint 2 — Identify which search methods work

- [x] Split source portal from application method: Easy Apply, company site, recruiter, referral, agency, and inbound.
- [x] Add response count, median response time, applications per interview, and sample-confidence indicators by channel.
- [x] Align source filters with every source value present in stored data.
- [x] Add a guided cleanup path for `Direct/Unknown` records.
- [x] Detect likely duplicate applications across channels and canonical job URLs.
- [x] Add reviewed duplicate merging that preserves Gmail threads and status history.

## Sprint 3 — Opportunity and interview conversion

- [x] Add event-level milestones for submissions, responses, interview scheduling/attendance, offers, and rejections.
- [x] Backfill existing status history into idempotent activity events.
- [x] Track distinct interview rounds without creating duplicate applications.
- [x] Link recruiting opportunities to the applications they produce.
- [x] Add 1/3/6/12-month opportunity and interview conversion analytics.
- [x] Show attended interview outcomes by source portal and application method.
- [x] Add application-table filters for interview attendance and final outcome.

## Sprint 4 — Find the best-fit market segments

- Capture role family, seniority, company type, industry, location/work mode, resume variant, job age, and fit score.
- Add segment comparisons and surface high-performing combinations.
- Add interview-stage and outcome-reason analysis.

## Sprint 5 — Turn insights into weekly experiments

- Add weekly targets for qualified applications, warm outreach, referrals, and conversations.
- Add an experiment board with hypotheses, allocations, thresholds, and results.
- Generate dated recommendations from personal conversion data.

## Sprint 6 — Add external market context

- Add a dated Market Lens for India hiring, IT/GCC demand, specialization trends, and applicant competition.
- Show source and methodology for every external metric.
- Keep market indicators visually separate from personal performance and avoid unsupported benchmark comparisons.
