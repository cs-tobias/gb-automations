/**
 * Gmail → Notion sync — Configuration
 *
 * All constants and property-name maps live here so they're easy to tweak
 * without hunting through the rest of the codebase.
 *
 * Apps Script note: every .gs file in a project shares one global namespace,
 * so anything declared here is visible from every other file. No imports needed.
 */

// --- Notion API ---
const NOTION_API_BASE = 'https://api.notion.com/v1';
const NOTION_VERSION = '2022-06-28';

// --- Gmail / sync behaviour ---
const PROJECT_LABEL_PREFIX = '';
const LOOKBACK_DAYS = 30;
const MAX_THREADS_PER_RUN = 30;

// --- Anthropic Claude (used for reconstructing quoted email history) ---
const CLAUDE_API_BASE = 'https://api.anthropic.com/v1/messages';
const CLAUDE_MODEL = 'claude-sonnet-4-5';
const MAX_BODY_CHARS_FOR_LLM = 50000;

// --- Feature flags ---
const ENABLE_HISTORY_RECONSTRUCTION = false;

// --- Drive attachments ---
const ATTACHMENTS_FOLDER_NAME = 'Notion Email Attachments';
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;  // 25 MB

// --- Emails DB property names ---
// Change these here if you name them differently in Notion.
//
// Required properties on the Emails DB:
//   - Subject     (title)
//   - Thread ID   (rich_text)
//   - Message ID  (rich_text)   ← unique dedup key per row
//   - Project     (relation)    → Projects (top-level pages)
//   - Contacts    (relation)    → Contacts DB
//   - From        (rich_text)
//   - From Email  (email)
//   - Direction   (select)      with options: Incoming, Outgoing, Reconstructed
//   - Date        (date)
//   - Tags        (multi-select, empty for now)
//   - Preview     (rich_text)
//   - Files       (files)
const EMAILS_PROPS = {
  subject:    'Subject',
  threadId:   'Thread ID',
  messageId:  'Message ID',
  project:    'Project',
  contacts:   'Contacts',
  from:       'From',
  fromEmail:  'From Email',
  direction:  'Direction',
  date:       'Date',
  tags:       'Tags',
  preview:    'Preview',
  files:      'Files',
};