-- Older legacy schema: what an early database looked like before DataStore._migrate_schema
-- added application.application_method / withdraw_reason and prospect.application_id, and
-- before applicationevent / applicationthreadid existed. Derived from pre_phase1_schema.sql.

CREATE TABLE application (
	id INTEGER NOT NULL, 
	company VARCHAR, 
	role VARCHAR, 
	source_portal VARCHAR NOT NULL, 
	job_url VARCHAR, 
	applied_date DATETIME NOT NULL, 
	current_status VARCHAR(21) NOT NULL, 
	thread_ids VARCHAR NOT NULL, 
	is_false_positive BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE suppressrule (
	id INTEGER NOT NULL, 
	sender_pattern VARCHAR NOT NULL, 
	subject_pattern VARCHAR, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE pollerstate (
	id INTEGER NOT NULL, 
	last_history_id VARCHAR, 
	last_sync_at DATETIME, 
	status VARCHAR NOT NULL, 
	error_message VARCHAR, 
	PRIMARY KEY (id)
);

CREATE TABLE processedmessage (
	message_id VARCHAR NOT NULL, 
	processed_at DATETIME NOT NULL, 
	result VARCHAR NOT NULL, 
	PRIMARY KEY (message_id)
);

CREATE TABLE statushistory (
	id INTEGER NOT NULL, 
	application_id INTEGER NOT NULL, 
	from_status VARCHAR, 
	to_status VARCHAR NOT NULL, 
	"trigger" VARCHAR NOT NULL, 
	changed_at DATETIME NOT NULL, 
	message_id VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(application_id) REFERENCES application (id)
);

CREATE TABLE prospect (
	id INTEGER NOT NULL, 
	source_portal VARCHAR NOT NULL, 
	category VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	sender VARCHAR NOT NULL, 
	snippet VARCHAR, 
	received_at DATETIME NOT NULL, 
	gmail_message_id VARCHAR NOT NULL, 
	gmail_thread_id VARCHAR NOT NULL, 
	status VARCHAR(9) NOT NULL, 
	classification_reason VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_application_current_status ON application (current_status);

CREATE INDEX ix_application_updated_at ON application (updated_at);

CREATE INDEX ix_application_source_portal ON application (source_portal);

CREATE INDEX ix_application_applied_date ON application (applied_date);

CREATE INDEX ix_prospect_received_at ON prospect (received_at);

CREATE INDEX ix_prospect_status ON prospect (status);

CREATE INDEX ix_prospect_category ON prospect (category);

CREATE INDEX ix_prospect_gmail_thread_id ON prospect (gmail_thread_id);

CREATE INDEX ix_prospect_updated_at ON prospect (updated_at);

CREATE INDEX ix_prospect_source_portal ON prospect (source_portal);

CREATE UNIQUE INDEX ix_prospect_gmail_message_id ON prospect (gmail_message_id);
