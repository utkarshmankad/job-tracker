-- Schema produced by origin/main @ e674e01 (SQLModel.metadata.create_all + DataStore._migrate_schema)
-- on a fresh database. Used by tests/unit/test_migrations.py as the pre-Phase-1 baseline.

CREATE TABLE application (
	id INTEGER NOT NULL, 
	company VARCHAR, 
	role VARCHAR, 
	source_portal VARCHAR NOT NULL, 
	application_method VARCHAR DEFAULT 'Unknown' NOT NULL, 
	job_url VARCHAR, 
	applied_date DATETIME NOT NULL, 
	current_status VARCHAR(21) NOT NULL, 
	thread_ids VARCHAR NOT NULL, 
	is_false_positive BOOLEAN NOT NULL, 
	withdraw_reason VARCHAR, 
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

CREATE TABLE applicationevent (
	id INTEGER NOT NULL, 
	application_id INTEGER NOT NULL, 
	event_type VARCHAR(21) NOT NULL, 
	occurred_at DATETIME NOT NULL, 
	interview_round VARCHAR(25), 
	source VARCHAR NOT NULL, 
	source_message_id VARCHAR, 
	status_history_id INTEGER, 
	notes VARCHAR, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(application_id) REFERENCES application (id)
);

CREATE TABLE applicationthreadid (
	id INTEGER NOT NULL, 
	application_id INTEGER NOT NULL, 
	thread_id VARCHAR NOT NULL, 
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
	application_id INTEGER, 
	status VARCHAR(9) NOT NULL, 
	classification_reason VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(application_id) REFERENCES application (id)
);

CREATE INDEX ix_application_current_status ON application (current_status);

CREATE INDEX ix_application_updated_at ON application (updated_at);

CREATE INDEX ix_application_application_method ON application (application_method);

CREATE INDEX ix_application_source_portal ON application (source_portal);

CREATE INDEX ix_application_applied_date ON application (applied_date);

CREATE INDEX ix_applicationevent_occurred_at ON applicationevent (occurred_at);

CREATE INDEX ix_applicationevent_event_type ON applicationevent (event_type);

CREATE UNIQUE INDEX ix_applicationevent_status_history_id ON applicationevent (status_history_id);

CREATE INDEX ix_applicationevent_source_message_id ON applicationevent (source_message_id);

CREATE INDEX ix_applicationevent_application_id ON applicationevent (application_id);

CREATE INDEX ix_applicationthreadid_application_id ON applicationthreadid (application_id);

CREATE INDEX ix_applicationthreadid_thread_id ON applicationthreadid (thread_id);

CREATE INDEX ix_prospect_received_at ON prospect (received_at);

CREATE INDEX ix_prospect_status ON prospect (status);

CREATE INDEX ix_prospect_category ON prospect (category);

CREATE INDEX ix_prospect_application_id ON prospect (application_id);

CREATE INDEX ix_prospect_gmail_thread_id ON prospect (gmail_thread_id);

CREATE INDEX ix_prospect_updated_at ON prospect (updated_at);

CREATE INDEX ix_prospect_source_portal ON prospect (source_portal);

CREATE UNIQUE INDEX ix_prospect_gmail_message_id ON prospect (gmail_message_id);
