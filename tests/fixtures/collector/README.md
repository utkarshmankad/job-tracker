# Collector adapter fixtures (synthetic)

Hand-authored HTML used by `tests/unit/test_collector_adapters.py`. Every company,
role, ID, date and URL is invented (the company names are Microsoft's documented sample
names). The files contain no real person, account, employer record, message, cookie or
tracking value. They model the *structure* each adapter expects; they were **not**
captured from the live sites, so passing these tests does not prove the selectors match
the live pages (see `collector/adapters/sites.py`).

Per source: `page1.html` and `page2.html` (pagination / lazy loading), `empty.html`,
`signed_out.html`, `challenge.html`, `drift.html` (a changed layout the adapter must
refuse). Replace a fixture only with another sanitized, synthetic page.
