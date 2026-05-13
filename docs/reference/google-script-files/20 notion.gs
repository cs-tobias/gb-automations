/**
 * Gmail → Notion sync — Notion API + Contacts
 *
 * Everything that talks to the Notion REST API lives here:
 *   - Emails DB row CRUD + dedup query
 *   - Project map (top-level pages, used to match Gmail labels)
 *   - Contacts DB upsert
 *   - Gmail label sync (creates a Gmail label per Notion project)
 *   - Generic block-append helper
 */

// ===========================================
// Emails DB
// ===========================================

/**
 * Query the Emails DB for a row matching this Gmail message ID.
 * Returns the Notion page object or null.
 */
function findEmailRowByMessageId(messageId) {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  const dbId = PropertiesService.getScriptProperties().getProperty('EMAILS_DB_ID');
  if (!dbId) throw new Error('EMAILS_DB_ID not set in Script Properties');

  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/databases/${dbId}/query`, {
    method: 'post',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({
      filter: { property: EMAILS_PROPS.messageId, rich_text: { equals: messageId } },
      page_size: 5,
    }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    throw new Error(`Emails DB query failed: ${response.getContentText()}`);
  }
  const data = JSON.parse(response.getContentText());
  const live = (data.results || []).filter(p => !p.archived && !p.in_trash);
  return live[0] || null;
}

/**
 * Quick existence check: does the Emails DB have ANY row with this Thread ID?
 * Used to decide whether to do LLM history reconstruction (only on first encounter).
 */
function hasAnyRowForThread(threadId) {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  const dbId = PropertiesService.getScriptProperties().getProperty('EMAILS_DB_ID');
  if (!dbId) throw new Error('EMAILS_DB_ID not set in Script Properties');

  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/databases/${dbId}/query`, {
    method: 'post',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({
      filter: { property: EMAILS_PROPS.threadId, rich_text: { equals: threadId } },
      page_size: 1,
    }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    console.error(`hasAnyRowForThread query failed: ${response.getContentText()}`);
    return false;
  }
  const data = JSON.parse(response.getContentText());
  const live = (data.results || []).filter(p => !p.archived && !p.in_trash);
  return live.length > 0;
}

function createEmailRow(properties) {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  const dbId = PropertiesService.getScriptProperties().getProperty('EMAILS_DB_ID');
  if (!dbId) throw new Error('EMAILS_DB_ID not set in Script Properties');

  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/pages`, {
    method: 'post',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({
      parent: { database_id: dbId },
      properties,
    }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    throw new Error(`Email row create failed: ${response.getContentText()}`);
  }
  return JSON.parse(response.getContentText());
}

// ===========================================
// Project map (top-level Notion pages)
// ===========================================

function getProjectMap() {
  const cache = PropertiesService.getScriptProperties();
  const cached = cache.getProperty('PROJECT_MAP');
  const cachedAt = parseInt(cache.getProperty('PROJECT_MAP_AT') || '0', 10);
  if (cached && Date.now() - cachedAt < 60 * 60 * 1000) return JSON.parse(cached);

  const map = fetchNotionPages();
  cache.setProperty('PROJECT_MAP', JSON.stringify(map));
  cache.setProperty('PROJECT_MAP_AT', String(Date.now()));
  return map;
}

function fetchNotionPages() {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  if (!token) throw new Error('NOTION_TOKEN not set');
  const contactsDbId = (PropertiesService.getScriptProperties().getProperty('CONTACTS_DB_ID') || '').replace(/-/g, '');
  const emailsDbId = (PropertiesService.getScriptProperties().getProperty('EMAILS_DB_ID') || '').replace(/-/g, '');

  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/search`, {
    method: 'post',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({ filter: { value: 'page', property: 'object' }, page_size: 100 }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    throw new Error(`Notion search failed: ${response.getContentText()}`);
  }

  const data = JSON.parse(response.getContentText());
  const map = {};
  for (const page of data.results) {
    // Skip pages that belong to the Contacts or Emails database
    const parent = page.parent || {};
    if (parent.type === 'database_id') {
      const parentDbId = (parent.database_id || '').replace(/-/g, '');
      if (parentDbId === contactsDbId) continue;
      if (parentDbId === emailsDbId) continue;
    }
    const title = extractPageTitle(page);
    if (title) map[title] = page.id;
  }
  return map;
}

function extractPageTitle(page) {
  const props = page.properties || {};
  for (const key of Object.keys(props)) {
    const prop = props[key];
    if (prop.type === 'title' && prop.title.length > 0) {
      return prop.title.map(t => t.plain_text).join('');
    }
  }
  return null;
}

/**
 * For every Notion page the integration can see, ensure a matching Gmail label exists.
 * Creates labels on the fly. Never deletes anything.
 */
function syncNotionProjectsToGmailLabels() {
  // Force fresh fetch — bypass the 1-hour cache so new Notion pages are picked up quickly
  PropertiesService.getScriptProperties().deleteProperty('PROJECT_MAP_AT');

  const projectMap = getProjectMap();
  let created = 0;

  for (const projectName of Object.keys(projectMap)) {
    if (!GmailApp.getUserLabelByName(projectName)) {
      GmailApp.createLabel(projectName);
      console.log(`Created Gmail label: ${projectName}`);
      created++;
    }
  }

  if (created === 0) {
    console.log(`All ${Object.keys(projectMap).length} Notion projects already have matching Gmail labels.`);
  } else {
    console.log(`Created ${created} new label(s).`);
  }
}

// ===========================================
// Generic block append
// ===========================================

function appendBlocksToPage(pageId, blocks) {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/blocks/${pageId}/children`, {
    method: 'patch',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({ children: blocks }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    throw new Error(`Notion append failed: ${response.getContentText()}`);
  }
}

// ===========================================
// Contacts DB
// ===========================================

/**
 * Extract every unique participant from a thread and upsert them to Notion Contacts.
 * Returns a MAP of email → Notion contact page ID. Per-message rows pick just the
 * contacts relevant to that message.
 */
function extractContactsFromThread(thread) {
  const seen = new Map();
  const messages = thread.getMessages();

  for (const msg of messages) {
    const headerParticipants = [
      msg.getFrom(),
      msg.getTo(),
      msg.getCc(),
    ].join(',').split(',');

    for (const raw of headerParticipants) {
      const parsed = parseParticipant(raw);
      if (!parsed) continue;
      if (isInternal(parsed.email)) continue;
      if (!seen.has(parsed.email)) {
        seen.set(parsed.email, {
          name: parsed.name,
          email: parsed.email,
          phone: null,
          company: companyFromDomain(parsed.email),
        });
      } else if (parsed.name && !seen.get(parsed.email).name) {
        seen.get(parsed.email).name = parsed.name;
      }
    }

    // Signature belongs to this message's sender (extractSignatureBlock cuts at
    // forward/reply markers, so we won't extract a quoted person's signature).
    const sigBlock = extractSignatureBlock(msg.getPlainBody());
    if (sigBlock) {
      const phone = extractPhone(sigBlock);
      const senderEmail = findSenderEmail(msg.getFrom());
      if (phone && senderEmail && seen.has(senderEmail)) {
        const contact = seen.get(senderEmail);
        if (!contact.phone) contact.phone = phone;
      }
    }
  }

  // Upsert each contact and build the email → pageId map
  const emailToPageId = {};
  for (const contact of seen.values()) {
    try {
      const pageId = upsertContact(contact);
      if (pageId) emailToPageId[contact.email] = pageId;
    } catch (err) {
      console.error(`Failed to upsert contact ${contact.email}: ${err}`);
    }
  }
  return emailToPageId;
}

function upsertContact(contact) {
  const dbId = PropertiesService.getScriptProperties().getProperty('CONTACTS_DB_ID');
  if (!dbId) throw new Error('CONTACTS_DB_ID not set in Script Properties');

  // If we never found a real name, fall back to the email as the title.
  if (!contact.name) contact.name = contact.email;

  const existing = findContactByEmail(dbId, contact.email);
  if (existing) {
    // Enrich: only add phone if existing has none
    const existingPhone = existing.properties.Phone?.phone_number;
    if (contact.phone && !existingPhone) {
      patchContact(existing.id, { Phone: { phone_number: contact.phone } });
      console.log(`  Updated phone for ${contact.email}`);
    }
    return existing.id;
  }

  const created = createContact(dbId, contact);
  console.log(`  Created contact: ${contact.name} (${contact.email})`);
  return created.id;
}

function findContactByEmail(dbId, email) {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/databases/${dbId}/query`, {
    method: 'post',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({
      filter: { property: 'Email', email: { equals: email } },
      page_size: 1,
    }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    throw new Error(`Notion contact query failed: ${response.getContentText()}`);
  }
  const data = JSON.parse(response.getContentText());
  return data.results[0] || null;
}

function createContact(dbId, contact) {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  const properties = {
    Name: { title: [{ text: { content: contact.name } }] },
    Email: { email: contact.email },
  };
  if (contact.phone) properties.Phone = { phone_number: contact.phone };
  if (contact.company) properties.Company = { rich_text: [{ text: { content: contact.company } }] };

  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/pages`, {
    method: 'post',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({
      parent: { database_id: dbId },
      properties,
    }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    throw new Error(`Notion contact create failed: ${response.getContentText()}`);
  }
  return JSON.parse(response.getContentText());
}

function patchContact(pageId, properties) {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  const response = UrlFetchApp.fetch(`${NOTION_API_BASE}/pages/${pageId}`, {
    method: 'patch',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Notion-Version': NOTION_VERSION,
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({ properties }),
    muteHttpExceptions: true,
  });
  if (response.getResponseCode() !== 200) {
    throw new Error(`Notion contact patch failed: ${response.getContentText()}`);
  }
}