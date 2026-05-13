/**
 * Gmail → Notion sync — Sync orchestration
 *
 * The entrypoint syncGmailToNotion() runs on a time-driven trigger.
 *
 * Architecture: one Notion row per Gmail MESSAGE (changed from v2's
 * one-row-per-thread). Each row links to Project + Contacts. The page body
 * contains a single chat-style callout for visual consistency.
 *
 * Sync state shape: { messageId: { rowPageId, threadId } }
 * Multi-user safe: Notion is the source of truth for "what's been synced."
 * Local syncState is just a cache to avoid querying Notion every run.
 */

function syncGmailToNotion() {
  syncNotionProjectsToGmailLabels();  // ensure labels exist for any new Notion projects
  const projectMap = getProjectMap();
  const syncState = getSyncState();
  const myEmail = Session.getActiveUser().getEmail().toLowerCase();

  for (const projectName of Object.keys(projectMap)) {
    const labelName = PROJECT_LABEL_PREFIX + projectName;
    const label = GmailApp.getUserLabelByName(labelName);
    if (!label) {
      console.log(`No Gmail label found for: ${labelName}`);
      continue;
    }

    const query = `label:"${labelName}" newer_than:${LOOKBACK_DAYS}d`;
    const threads = GmailApp.search(query, 0, MAX_THREADS_PER_RUN);
    console.log(`${projectName}: scanning ${threads.length} threads`);

    for (const thread of threads) {
      try {
        const newCount = syncThreadMessages(thread, projectMap[projectName], syncState, myEmail);
        if (newCount > 0) {
          console.log(`  ${thread.getFirstMessageSubject()}: +${newCount} new row(s)`);
        }
        // Persist after each successful thread so a later failure doesn't lose this progress
        saveSyncState(syncState);
      } catch (err) {
        console.error(`Failed thread ${thread.getId()}: ${err}`);
        saveSyncState(syncState);
      }
    }
  }

  saveSyncState(syncState);
}

/**
 * Sync one Gmail thread by creating one Notion row per message.
 *
 * Flow per message:
 *   1. Check local cache by messageId.
 *   2. If miss, query Notion for an existing row with this Message ID.
 *   3. If neither, create a new row and cache it.
 *
 * Returns the number of new rows created.
 */
function syncThreadMessages(thread, projectPageId, syncState, myEmail) {
  const threadId = thread.getId();
  const subject = thread.getFirstMessageSubject() || '(no subject)';
  const messages = thread.getMessages();

  console.log(`  Thread ${threadId} ("${subject}"): ${messages.length} message(s) in Gmail`);

  // Extract contacts once per thread for efficiency; we'll filter per-message below
  // when deciding which contacts to link on each row.
  const contactPageIdByEmail = extractContactsFromThread(thread);

  let newRows = 0;

  // History reconstruction: if this is the very first time we're seeing this thread
  // AND the first message contains quoted history, reconstruct it as standalone rows.
  newRows += maybeReconstructHistory({
    thread, threadId, subject, messages, projectPageId,
    contactPageIdByEmail, syncState, myEmail,
  });

  // Now sync each real Gmail message as its own row
  for (const msg of messages) {
    const messageId = msg.getId();

    // Step 1: local cache hit?
    if (syncState[messageId]) continue;

    // Step 2: Notion already has a row for this message (another user synced it)?
    const existing = findEmailRowByMessageId(messageId);
    if (existing) {
      syncState[messageId] = { rowPageId: existing.id, threadId };
      continue;
    }

    // Step 3: create a new row
    try {
      const rowId = createMessageRow({
        msg, threadId, subject, projectPageId, contactPageIdByEmail, myEmail,
      });
      syncState[messageId] = { rowPageId: rowId, threadId };
      newRows++;
    } catch (err) {
      console.error(`  Failed to create row for message ${messageId}: ${err}`);
    }
  }

  return newRows;
}

/**
 * Has any message from this thread been recorded in our local cache?
 */
function anyMessageFromThreadInState(syncState, threadId) {
  for (const key of Object.keys(syncState)) {
    if (syncState[key] && syncState[key].threadId === threadId) return true;
  }
  return false;
}

/**
 * Create a Notion row for a real Gmail message, including its callout body
 * and any attachments uploaded to Drive. Returns the new row's page ID.
 */
function createMessageRow({ msg, threadId, subject, projectPageId, contactPageIdByEmail, myEmail }) {
  const messageId = msg.getId();
  const fromField = msg.getFrom();
  const fromEmail = extractEmail(fromField).toLowerCase();
  const fromName = extractName(fromField);
  const isOutgoing = fromEmail === myEmail;
  const date = msg.getDate();

  // Which contacts should we link on this row?
  // - Always include the sender (if they're a known external contact)
  // - Include external recipients (so filtering on "messages involving X" works)
  const participantEmails = collectParticipantEmails(msg);
  const linkedContactIds = participantEmails
    .map(e => contactPageIdByEmail[e])
    .filter(Boolean);

  // Upload any attachments on this specific message
  const files = uploadAttachmentsFromMessages([msg]);

  const preview = makePreview(msg);

  const props = buildMessageRowProps({
    subject, threadId, messageId, projectPageId,
    contactPageIds: linkedContactIds,
    fromName, fromEmail,
    direction: isOutgoing ? 'Outgoing' : 'Incoming',
    date, preview, files,
  });

  const created = createEmailRow(props);
  const rowPageId = created.id;

  // Append the chat-style callout to the row's page body
  const blocks = messageToChatBlocks(msg, myEmail);
  for (const batch of chunkArray(blocks, 100)) {
    appendBlocksToPage(rowPageId, batch);
  }

  return rowPageId;
}

/**
 * Collect all distinct participant emails on a single message (from/to/cc),
 * lowercased and external-filtered (we drop internal-domain addresses).
 */
function collectParticipantEmails(msg) {
  const all = [msg.getFrom(), msg.getTo(), msg.getCc()].join(',').split(',');
  const out = new Set();
  for (const raw of all) {
    const parsed = parseParticipant(raw);
    if (!parsed) continue;
    if (isInternal(parsed.email)) continue;
    out.add(parsed.email);
  }
  return Array.from(out);
}

/**
 * Build the Notion properties object for a single-message Emails DB row.
 */
function buildMessageRowProps({ subject, threadId, messageId, projectPageId, contactPageIds, fromName, fromEmail, direction, date, preview, files }) {
  const props = {};

  props[EMAILS_PROPS.subject] = {
    title: [{ text: { content: (subject || '(no subject)').slice(0, 1900) } }],
  };
  props[EMAILS_PROPS.threadId] = {
    rich_text: [{ text: { content: threadId } }],
  };
  props[EMAILS_PROPS.messageId] = {
    rich_text: [{ text: { content: messageId } }],
  };
  props[EMAILS_PROPS.project] = {
    relation: projectPageId ? [{ id: projectPageId }] : [],
  };
  props[EMAILS_PROPS.contacts] = {
    relation: (contactPageIds || []).map(id => ({ id })),
  };
  if (fromName) {
    props[EMAILS_PROPS.from] = {
      rich_text: [{ text: { content: fromName.slice(0, 1900) } }],
    };
  }
  if (fromEmail) {
    // Synthetic reconstructed rows may have no fromEmail — skip in that case.
    props[EMAILS_PROPS.fromEmail] = { email: fromEmail };
  }
  if (direction) {
    props[EMAILS_PROPS.direction] = { select: { name: direction } };
  }
  if (date) {
    props[EMAILS_PROPS.date] = { date: { start: date.toISOString() } };
  }
  if (preview) {
    props[EMAILS_PROPS.preview] = {
      rich_text: [{ text: { content: preview.slice(0, 1900) } }],
    };
  }
  if (files && files.length > 0) {
    props[EMAILS_PROPS.files] = {
      files: files.slice(0, 100).map(f => ({
        name: (f.name || 'attachment').slice(0, 100),
        external: { url: f.url },
      })),
    };
  }
  return props;
}

/**
 * One-line preview from a cleaned message body.
 */
function makePreview(msg) {
  const cleaned = cleanBody(msg.getPlainBody());
  if (!cleaned) return '';
  const firstLine = cleaned.split('\n').find(l => l.trim()) || '';
  return firstLine.slice(0, 200);
}

/**
 * Turn one Gmail message into a chat-style group of blocks for the row's page body.
 */
function messageToChatBlocks(msg, myEmail) {
  const from = msg.getFrom();
  const fromEmail = extractEmail(from).toLowerCase();
  const fromName = extractName(from);
  const isOutgoing = fromEmail === myEmail;

  const date = msg.getDate();
  const timestamp = Utilities.formatDate(date, Session.getScriptTimeZone(), 'MMM d, yyyy · HH:mm');

  let body = cleanBody(msg.getPlainBody());
  if (!body) body = '_(no message body — forwarded or empty)_';

  const blocks = [];

  blocks.push({
    object: 'block',
    type: 'paragraph',
    paragraph: {
      rich_text: [{
        type: 'text',
        text: { content: timestamp },
        annotations: { italic: true, color: 'gray' },
      }],
    },
  });

  const bodyChunks = chunkText(body, 1900);
  const calloutRichText = [
    { type: 'text', text: { content: fromName + '\n' }, annotations: { bold: true } },
    { type: 'text', text: { content: bodyChunks[0] || '' } },
  ];

  blocks.push({
    object: 'block',
    type: 'callout',
    callout: {
      icon: { type: 'emoji', emoji: isOutgoing ? '💬' : '📨' },
      color: isOutgoing ? 'blue_background' : 'gray_background',
      rich_text: calloutRichText,
    },
  });

  for (let i = 1; i < bodyChunks.length; i++) {
    blocks.push(paragraphBlock(bodyChunks[i]));
  }

  return blocks;
}

// ===========================================
// Quoted history reconstruction (Claude)
// ===========================================

/**
 * Top-level history-reconstruction step. Called from syncThreadMessages.
 * Returns the number of reconstructed rows created.
 */
function maybeReconstructHistory({ thread, threadId, subject, messages, projectPageId, contactPageIdByEmail, syncState, myEmail }) {
  if (!ENABLE_HISTORY_RECONSTRUCTION) return 0;
  const firstMsg = messages[0];
  if (!firstMsg) return 0;

  // Only reconstruct on the very first encounter with this thread.
  // Local-cache check first (cheap); fall back to Notion check (one HTTP call).
  if (anyMessageFromThreadInState(syncState, threadId)) return 0;
  if (hasAnyRowForThread(threadId)) return 0;

  const rawBody = firstMsg.getPlainBody();
  if (!rawBody || !hasQuotedHistoryHint(rawBody)) return 0;

  let reconstructed;
  try {
    reconstructed = reconstructQuotedHistory(rawBody);
  } catch (err) {
    console.error(`  History reconstruction failed: ${err}`);
    return 0;
  }
  if (!reconstructed || reconstructed.length === 0) return 0;

  console.log(`  Reconstructed ${reconstructed.length} historical message(s) from quoted chain`);
  let created = 0;
  for (let i = 0; i < reconstructed.length; i++) {
    const histMsg = reconstructed[i];
    const syntheticId = `recon:${threadId}:${i}`;

    if (syncState[syntheticId]) continue;
    const existingRecon = findEmailRowByMessageId(syntheticId);
    if (existingRecon) {
      syncState[syntheticId] = { rowPageId: existingRecon.id, threadId };
      continue;
    }

    try {
      const rowId = createReconstructedMessageRow({
        histMsg, syntheticId, threadId, subject, projectPageId,
        contactPageIdByEmail, myEmail,
      });
      syncState[syntheticId] = { rowPageId: rowId, threadId };
      created++;
    } catch (err) {
      console.error(`  Failed to create reconstructed row ${syntheticId}: ${err}`);
    }
  }
  return created;
}

/**
 * Cheap regex pre-check: does this body contain markers suggesting quoted history?
 * False positives cost an LLM call; false negatives lose history.
 */
function hasQuotedHistoryHint(rawBody) {
  if (!rawBody) return false;
  const markers = [
    /\bOn .+ wrote:/i,
    /\bDen\s+.+\s+skrev\s+/i,
    /\bkl\.\s+\d{1,2}[:.]\d{2},?\s+skrev\s+/i,
    /-{3,}\s*Forwarded message\s*-{3,}/i,
    /-{3,}\s*Original Message\s*-{3,}/i,
    /^Fra:\s+.+/im,
    /^From:\s+.+@.+/im,
  ];
  return markers.some(re => re.test(rawBody));
}

/**
 * Ask Claude to parse a raw email body containing quoted history into a list of
 * structured messages, deduplicated and in chronological order.
 *
 * Returns: array of { sender, email, date (ISO or null), body } or null on failure.
 */
function reconstructQuotedHistory(rawBody) {
  const truncated = rawBody.length > MAX_BODY_CHARS_FOR_LLM
    ? rawBody.slice(0, MAX_BODY_CHARS_FOR_LLM)
    : rawBody;

  const prompt = `You are parsing an email that contains quoted history from prior messages in the same thread.

Your job: extract every UNIQUE prior message from the quoted history (the "On X wrote:" / "Den ... skrev:" / "Fra: ..." sections), in chronological order (oldest first). Deduplicate — nested quotes mean the same message appears multiple times; include each unique message ONLY ONCE.

CRITICAL RULES:
- Do NOT include the top-level message (the new content above the first quote marker). We already have that.
- Each extracted message's body should be just THAT person's own words, NOT including the further-nested quotes inside it.
- Strip signatures (Mvh, Best regards, Med vennlig hilsen, --, etc.) from bodies.
- Strip ">" quote prefixes and similar artifacts.
- If you cannot determine a sender or date with reasonable confidence, use null.
- Output ONLY valid JSON, no preamble, no markdown fences.

Output schema:
{
  "messages": [
    { "sender": "Display Name or null", "email": "person@x.com or null", "date": "ISO 8601 datetime or null", "body": "the person's own words, cleaned" }
  ]
}

If no prior messages can be reliably extracted, return {"messages": []}.

Email body to parse:
---
${truncated}
---`;

  const apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
  if (!apiKey) {
    console.warn('ANTHROPIC_API_KEY not set — skipping history reconstruction');
    return null;
  }

  const response = UrlFetchApp.fetch(CLAUDE_API_BASE, {
    method: 'post',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'Content-Type': 'application/json',
    },
    payload: JSON.stringify({
      model: CLAUDE_MODEL,
      max_tokens: 4096,
      messages: [{ role: 'user', content: prompt }],
    }),
    muteHttpExceptions: true,
  });

  if (response.getResponseCode() !== 200) {
    console.error(`Claude API error: ${response.getContentText()}`);
    return null;
  }

  const data = JSON.parse(response.getContentText());
  const text = data.content?.[0]?.text;
  if (!text) return null;

  const cleaned = text.replace(/^```(?:json)?\s*/i, '').replace(/\s*```\s*$/i, '').trim();

  let parsed;
  try {
    parsed = JSON.parse(cleaned);
  } catch (err) {
    console.error(`Failed to parse Claude response as JSON: ${cleaned.slice(0, 500)}`);
    return null;
  }

  const messages = parsed.messages || [];
  if (!Array.isArray(messages)) return null;
  return messages;
}

/**
 * Create a Notion row for an LLM-reconstructed historical message.
 * Returns the new row's page ID.
 */
function createReconstructedMessageRow({ histMsg, syntheticId, threadId, subject, projectPageId, contactPageIdByEmail, myEmail }) {
  const fromEmail = (histMsg.email || '').toLowerCase();
  const fromName = histMsg.sender || histMsg.email || 'Unknown sender';

  // Use the parsed date if available, otherwise fall back to "now" so the row
  // is at least placeable — you can identify it as reconstructed via Direction.
  let date;
  if (histMsg.date) {
    const d = new Date(histMsg.date);
    date = isNaN(d.getTime()) ? new Date() : d;
  } else {
    date = new Date();
  }

  // Try to link a contact if we recognize the sender's email
  const linkedContactIds = [];
  if (fromEmail && contactPageIdByEmail[fromEmail]) {
    linkedContactIds.push(contactPageIdByEmail[fromEmail]);
  }

  const body = (histMsg.body || '').trim();
  const preview = body.split('\n').find(l => l.trim()) || '';

  const props = buildMessageRowProps({
    subject, threadId, messageId: syntheticId, projectPageId,
    contactPageIds: linkedContactIds,
    fromName, fromEmail,
    direction: 'Reconstructed',
    date, preview, files: [],
  });

  const created = createEmailRow(props);
  const rowPageId = created.id;

  const blocks = reconstructedMessageToChatBlocks(histMsg, myEmail);
  for (const batch of chunkArray(blocks, 100)) {
    appendBlocksToPage(rowPageId, batch);
  }

  return rowPageId;
}

/**
 * Render an LLM-reconstructed message as chat blocks. The Direction property
 * on the row marks it as reconstructed; here we use a distinct 📜 icon.
 */
function reconstructedMessageToChatBlocks(histMsg, myEmail) {
  const fromEmail = (histMsg.email || '').toLowerCase();
  const fromName = histMsg.sender || histMsg.email || 'Unknown sender';
  const isOutgoing = fromEmail && fromEmail === myEmail;

  let timestamp = '(historical — reconstructed)';
  if (histMsg.date) {
    try {
      const d = new Date(histMsg.date);
      if (!isNaN(d.getTime())) {
        timestamp = Utilities.formatDate(d, Session.getScriptTimeZone(), 'MMM d, yyyy · HH:mm') + ' (reconstructed)';
      }
    } catch (_) { /* keep default */ }
  }

  const body = (histMsg.body || '').trim() || '_(empty)_';
  const blocks = [];

  blocks.push({
    object: 'block',
    type: 'paragraph',
    paragraph: {
      rich_text: [{
        type: 'text',
        text: { content: timestamp },
        annotations: { italic: true, color: 'gray' },
      }],
    },
  });

  const bodyChunks = chunkText(body, 1900);
  const calloutRichText = [
    { type: 'text', text: { content: fromName + '\n' }, annotations: { bold: true } },
    { type: 'text', text: { content: bodyChunks[0] || '' } },
  ];

  blocks.push({
    object: 'block',
    type: 'callout',
    callout: {
      icon: { type: 'emoji', emoji: '📜' },
      color: isOutgoing ? 'blue_background' : 'gray_background',
      rich_text: calloutRichText,
    },
  });

  for (let i = 1; i < bodyChunks.length; i++) {
    blocks.push(paragraphBlock(bodyChunks[i]));
  }

  return blocks;
}