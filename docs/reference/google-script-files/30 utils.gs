/**
 * Gmail → Notion sync — Utilities
 *
 * Everything that isn't sync orchestration or a Notion HTTP call:
 *   - Email body cleaning, participant parsing, phone extraction
 *   - Drive attachment upload + signature-region filtering
 *   - Notion block helpers (paragraph, heading, divider)
 *   - Text/array chunking
 *   - Sync-state persistence
 *   - Maintenance helpers (testConnection, resetSyncState, inspectSyncState, debug)
 */

// ===========================================
// Email body cleaning
// ===========================================

/**
 * Aggressively clean a Gmail message body down to just the new content.
 * Returns empty string if the message has no original content (pure forward/quote).
 */
function cleanBody(text) {
  if (!text) return '';

  // Step 0: normalize. Gmail sometimes flattens newlines into double-spaces;
  // restore them by treating any run of 2+ spaces/tabs as a newline.
  let normalized = text
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/[ \t]{2,}/g, '\n');

  let lines = normalized.split('\n');

  // Step 1: cut at the start of any quoted/forwarded section.
  const replyMarkers = [
    /^On .+ wrote:\s*$/i,
    /^.{3,}\s+kl\.\s+\d{1,2}[:.]\d{2},?\s+skrev\s+.+:?\s*$/i,  // Norwegian "skrev"
    /^Den\s+.+\s+skrev\s+.+:?\s*$/i,
    /^-{3,}\s*Forwarded message\s*-{3,}/i,
    /^-{3,}\s*Original Message\s*-{3,}/i,
    /^Fra:\s+.+/i,        // Norwegian "From:" header in forwards
    /^From:\s+.+/i,
    /^Sendt:\s+/i,
    /^Sent:\s+/i,
    /^_{5,}\s*$/,
  ];

  let cutIndex = lines.length;
  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].replace(/^>+\s?/g, '').trim();
    if (!stripped) continue;
    for (const marker of replyMarkers) {
      if (marker.test(stripped)) { cutIndex = i; break; }
    }
    if (cutIndex < lines.length) break;
  }
  lines = lines.slice(0, cutIndex);

  // Step 2: cut at signature.
  const signatureMarkers = [
    /^Med\s+vennlig\s+hilsen,?\s*$/i,
    /^Mvh\.?,?\s*$/i,
    /^Vennlig\s+hilsen,?\s*$/i,
    /^Vh,?\s*$/i,
    /^Best\s+regards,?\s*$/i,
    /^Kind\s+regards,?\s*$/i,
    /^Regards,?\s*$/i,
    /^Sincerely,?\s*$/i,
    /^Cheers,?\s*$/i,
    /^Thanks,?\s*$/i,
    /^Thx,?\s*$/i,
    /^--\s*$/,
    /^Sent from my (iPhone|iPad|Android|mobile)/i,
    /^Hilsen,?\s*$/i,
  ];

  cutIndex = lines.length;
  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].replace(/^>+\s?/g, '').trim();
    if (!stripped) continue;
    for (const marker of signatureMarkers) {
      if (marker.test(stripped)) { cutIndex = i; break; }
    }
    if (cutIndex < lines.length) break;
  }
  lines = lines.slice(0, cutIndex);

  // Step 3: strip residual quote markers and inline noise.
  lines = lines.map(line => line
    .replace(/^(>\s?)+/, '')
    .replace(/\[image:\s*[^\]]+\]/gi, '')
    .replace(/<http[^>]+>/g, '')
  );

  // Step 4: collapse whitespace and blank lines.
  lines = lines.map(l => l.replace(/\s+$/, ''));
  while (lines.length && lines[0].trim() === '') lines.shift();
  while (lines.length && lines[lines.length - 1].trim() === '') lines.pop();

  const collapsed = [];
  let blankRun = 0;
  for (const line of lines) {
    if (line.trim() === '') {
      blankRun++;
      if (blankRun <= 1) collapsed.push('');
    } else {
      blankRun = 0;
      collapsed.push(line);
    }
  }

  return collapsed.join('\n').trim();
}

// ===========================================
// Participant parsing
// ===========================================

function extractEmail(fromField) {
  const match = fromField.match(/<([^>]+)>/);
  return match ? match[1] : fromField.trim();
}

function extractName(fromField) {
  const match = fromField.match(/^([^<]+?)\s*</);
  if (match) return match[1].replace(/^["']|["']$/g, '').trim();
  return fromField.split('@')[0];
}

/**
 * Parse a "Name <email@host>" string. Returns null if no email found.
 * Strict about names: only uses the display name if present, otherwise null.
 */
function parseParticipant(raw) {
  if (!raw || !raw.trim()) return null;
  const emailMatch = raw.match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
  if (!emailMatch) return null;
  const email = emailMatch[0].toLowerCase();

  let name = '';
  const nameMatch = raw.match(/^([^<]+?)\s*<[^>]+>/);
  if (nameMatch) {
    name = nameMatch[1]
      .replace(/^["']|["']$/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  // Reject obviously bad names: empty, the email itself, or a generic alias.
  const lowerName = name.toLowerCase();
  const localPart = email.split('@')[0];
  const looksGeneric = ['post', 'info', 'contact', 'hello', 'admin', 'noreply', 'no-reply'].includes(lowerName);
  const isJustTheEmail = lowerName === email || lowerName === localPart;

  if (!name || looksGeneric || isJustTheEmail) {
    return { name: null, email };
  }
  return { name, email };
}

function isInternal(email) {
  const raw = PropertiesService.getScriptProperties().getProperty('INTERNAL_EMAILS_OR_DOMAINS') || '';
  const entries = raw.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  const e = email.toLowerCase();
  for (const entry of entries) {
    if (entry.includes('@')) {
      if (e === entry) return true;     // exact email match
    } else {
      if (e.endsWith('@' + entry)) return true;  // domain match
    }
  }
  return false;
}

function companyFromDomain(email) {
  const domain = email.split('@')[1] || '';
  const parts = domain.split('.');
  if (parts.length === 0) return '';
  // "ht@metropolis.no" → "Metropolis"; "person@sub.company.com" → "Company"
  const main = parts.length >= 2 ? parts[parts.length - 2] : parts[0];
  return main.charAt(0).toUpperCase() + main.slice(1);
}

function findSenderEmail(fromField) {
  const m = fromField.match(/<([^>]+)>/);
  return m ? m[1].toLowerCase() : fromField.trim().toLowerCase();
}

// ===========================================
// Signature + phone extraction
// ===========================================

/**
 * Pull the signature region out of the message body.
 * Only looks at the sender's own content — stops at the first forward/reply marker
 * so we never extract a quoted person's signature.
 */
function extractSignatureBlock(text) {
  if (!text) return '';

  const normalized = text
    .replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    .replace(/[ \t]{2,}/g, '\n');
  let lines = normalized.split('\n');

  // Step 1: trim away anything after a forward/reply marker.
  const cutMarkers = [
    /^On .+ wrote:\s*$/i,
    /^.{3,}\s+kl\.\s+\d{1,2}[:.]\d{2},?\s+skrev\s+.+:?\s*$/i,
    /^Den\s+.+\s+skrev\s+.+:?\s*$/i,
    /^-{3,}\s*Forwarded message\s*-{3,}/i,
    /^-{3,}\s*Original Message\s*-{3,}/i,
    /^Fra:\s+.+/i,
    /^From:\s+.+/i,
  ];
  let cutIdx = lines.length;
  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].replace(/^>+\s?/g, '').trim();
    if (!stripped) continue;
    for (const marker of cutMarkers) {
      if (marker.test(stripped)) { cutIdx = i; break; }
    }
    if (cutIdx < lines.length) break;
  }
  lines = lines.slice(0, cutIdx);

  // Step 2: find the signature start within the sender's own content.
  const sigStartRegex = /^(Med\s+vennlig\s+hilsen|Mvh\.?|Vennlig\s+hilsen|Best\s+regards|Kind\s+regards|Regards|Sincerely),?\s*$/i;
  let sigStart = -1;
  for (let i = 0; i < lines.length; i++) {
    if (sigStartRegex.test(lines[i].trim())) { sigStart = i; break; }
  }
  if (sigStart === -1) return '';

  return lines.slice(sigStart).join('\n');
}

/**
 * Extract a phone number from a chunk of text. Norwegian-friendly.
 * Handles +47 international, Norwegian 8-digit local, and labeled formats.
 * Prefers mobile numbers when both are present.
 */
function extractPhone(text) {
  if (!text) return null;
  const candidates = [];

  // International with country code (+ or with parens)
  const intlRegex = /\(?\+\s*\d{1,3}\)?[\s\-.]*\d[\d\s\-.]{6,14}\d/g;
  let m;
  while ((m = intlRegex.exec(text)) !== null) {
    candidates.push({ value: m[0], index: m.index, isMobile: isMobileContext(text, m.index) });
  }

  // Norwegian 8-digit local, various groupings
  const localRegex = /(?:^|[^\d+])(\d{2,3}[\s\-.]?\d{2,3}[\s\-.]?\d{2,3}[\s\-.]?\d{0,2})(?=$|[^\d])/g;
  while ((m = localRegex.exec(text)) !== null) {
    const digits = m[1].replace(/\D/g, '');
    if (digits.length === 8) {
      candidates.push({ value: m[1].trim(), index: m.index, isMobile: isMobileContext(text, m.index) });
    }
  }

  if (candidates.length === 0) return null;
  const mobile = candidates.find(c => c.isMobile);
  const chosen = mobile || candidates[0];
  return chosen.value.replace(/[\s\-.]+/g, ' ').trim();
}

function isMobileContext(text, matchIndex) {
  const context = text.substring(Math.max(0, matchIndex - 15), matchIndex).toLowerCase();
  return /(mob|mobil|m[:.]\s*$|cell)/i.test(context);
}

// ===========================================
// Drive attachment upload
// ===========================================

/**
 * Get (or create) the Drive folder where email attachments are stored.
 * Memoized via PropertiesService.
 */
function getAttachmentsFolder() {
  const props = PropertiesService.getScriptProperties();
  const cachedId = props.getProperty('ATTACHMENTS_FOLDER_ID');
  if (cachedId) {
    try {
      return DriveApp.getFolderById(cachedId);
    } catch (_) {
      // Folder was deleted or moved — fall through to recreate
    }
  }

  const folders = DriveApp.getFoldersByName(ATTACHMENTS_FOLDER_NAME);
  let folder;
  if (folders.hasNext()) {
    folder = folders.next();
  } else {
    folder = DriveApp.createFolder(ATTACHMENTS_FOLDER_NAME);
    console.log(`Created Drive folder: ${ATTACHMENTS_FOLDER_NAME}`);
  }

  props.setProperty('ATTACHMENTS_FOLDER_ID', folder.getId());
  return folder;
}

/**
 * Upload every attachment from the given list of Gmail messages to Drive.
 * Returns an array of { name, url } for each successfully uploaded file.
 *
 * Filters out attachments that appear in the signature region of the message
 * body (e.g. signature logos like image001.png appearing AFTER "Mvh").
 * Attachments not referenced in the body (= regular file attachments) are kept.
 */
function uploadAttachmentsFromMessages(messages) {
  const uploaded = [];
  let folder = null;  // lazy-init so we don't hit Drive when there are no attachments

  for (const msg of messages) {
    let attachments;
    try {
      attachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true });
    } catch (err) {
      console.error(`  Failed to read attachments for message ${msg.getId()}: ${err}`);
      continue;
    }
    if (!attachments || attachments.length === 0) continue;

    const sigStartLine = findSignatureStartLine(msg.getPlainBody());

    if (!folder) folder = getAttachmentsFolder();

    for (const att of attachments) {
      try {
        const name = att.getName() || 'attachment';

        // Position check: if this attachment is referenced in the body and
        // the reference appears at-or-after the signature line, skip it.
        if (sigStartLine !== -1) {
          const refLine = findAttachmentReferenceLine(msg.getPlainBody(), name);
          if (refLine !== -1 && refLine >= sigStartLine) {
            console.log(`  Skipping signature-region attachment: ${name}`);
            continue;
          }
        }

        const size = att.getSize();
        if (size > MAX_ATTACHMENT_BYTES) {
          console.warn(`  Skipping oversized attachment ${name} (${size} bytes)`);
          continue;
        }
        const file = folder.createFile(att.copyBlob());
        file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);

        const url = `https://drive.google.com/uc?export=view&id=${file.getId()}`;
        uploaded.push({ name, url });
      } catch (err) {
        console.error(`  Failed to upload attachment ${att.getName()}: ${err}`);
      }
    }
  }

  if (uploaded.length > 0) {
    console.log(`  Uploaded ${uploaded.length} attachment(s) to Drive`);
  }
  return uploaded;
}

/**
 * Return the line index where the "final" signature of the message starts,
 * or -1 if none found. Scans from the bottom up so we find the signature of
 * the message-as-received, not a signature from a forwarded segment higher up.
 */
function findSignatureStartLine(text) {
  if (!text) return -1;
  const normalized = text
    .replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    .replace(/[ \t]{2,}/g, '\n');
  const lines = normalized.split('\n');

  const sigStartRegex = /^(Med\s+vennlig\s+hilsen|Mvh\.?|Vennlig\s+hilsen|Vh|Best\s+regards|Kind\s+regards|Regards|Sincerely|Cheers|Hilsen),?\s*$/i;

  let lastMarkerIdx = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (sigStartRegex.test(lines[i].trim())) {
      lastMarkerIdx = i;
      break;
    }
  }
  return lastMarkerIdx;
}

/**
 * Find the line index in the message body where an attachment is referenced.
 * Looks for `[image: filename.png]` Gmail-plaintext markers.
 * Returns -1 if not referenced anywhere in the body.
 */
function findAttachmentReferenceLine(text, attachmentName) {
  if (!text || !attachmentName) return -1;
  const normalized = text
    .replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    .replace(/[ \t]{2,}/g, '\n');
  const lines = normalized.split('\n');
  const escaped = attachmentName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const ref = new RegExp(`\\[image:\\s*${escaped}\\s*\\]`, 'i');
  for (let i = 0; i < lines.length; i++) {
    if (ref.test(lines[i])) return i;
  }
  return -1;
}

// ===========================================
// Notion block helpers
// ===========================================

function paragraphBlock(text) {
  return { object: 'block', type: 'paragraph', paragraph: { rich_text: [{ type: 'text', text: { content: text } }] } };
}

function headingBlock(text, level) {
  const type = `heading_${level}`;
  return { object: 'block', type, [type]: { rich_text: [{ type: 'text', text: { content: text } }] } };
}

function dividerBlock() {
  return { object: 'block', type: 'divider', divider: {} };
}

// ===========================================
// Chunking
// ===========================================

function chunkText(text, size) {
  const chunks = [];
  for (let i = 0; i < text.length; i += size) chunks.push(text.slice(i, i + size));
  return chunks.length ? chunks : [''];
}

function chunkArray(arr, size) {
  const chunks = [];
  for (let i = 0; i < arr.length; i += size) chunks.push(arr.slice(i, i + size));
  return chunks;
}

// ===========================================
// Sync state persistence
// ===========================================

function getSyncState() {
  const raw = PropertiesService.getScriptProperties().getProperty('SYNC_STATE');
  return raw ? JSON.parse(raw) : {};
}

function saveSyncState(state) {
  const serialized = JSON.stringify(state);
  if (serialized.length > 450000) {
    console.warn(`SYNC_STATE is ${serialized.length} bytes — approaching 500KB limit. Consider migrating to Sheets.`);
  }
  PropertiesService.getScriptProperties().setProperty('SYNC_STATE', serialized);
}

// ===========================================
// Maintenance / debug helpers
// ===========================================

function testConnection() {
  console.log(JSON.stringify(fetchNotionPages(), null, 2));
}

/**
 * Wipe all sync state — next run will re-evaluate every thread against Notion.
 * Does NOT delete what's already in Notion.
 */
function resetSyncState() {
  PropertiesService.getScriptProperties().deleteProperty('SYNC_STATE');
  console.log('Sync state cleared.');
}

function inspectSyncState() {
  const state = getSyncState();
  console.log(`Tracking ${Object.keys(state).length} messages`);
  console.log(JSON.stringify(state, null, 2));
}

/**
 * Scan ALL labeled threads across ALL Notion projects and show what contacts
 * would be extracted. Read-only — does not write to Notion.
 */
function debugExtractContactsAll() {
  const projectMap = getProjectMap();
  const allContacts = new Map();

  for (const projectName of Object.keys(projectMap)) {
    const label = GmailApp.getUserLabelByName(projectName);
    if (!label) continue;

    const threads = GmailApp.search(`label:"${projectName}" newer_than:${LOOKBACK_DAYS}d`, 0, MAX_THREADS_PER_RUN);
    console.log(`\n=== ${projectName} (${threads.length} threads) ===`);

    for (const thread of threads) {
      const subject = thread.getFirstMessageSubject();
      const seen = new Map();

      for (const msg of thread.getMessages()) {
        for (const raw of [msg.getFrom(), msg.getTo(), msg.getCc()].join(',').split(',')) {
          const parsed = parseParticipant(raw);
          if (!parsed || isInternal(parsed.email)) continue;
          if (!seen.has(parsed.email)) {
            seen.set(parsed.email, {
              name: parsed.name, email: parsed.email, phone: null,
              company: companyFromDomain(parsed.email),
            });
          } else if (parsed.name && !seen.get(parsed.email).name) {
            seen.get(parsed.email).name = parsed.name;
          }
        }

        const sigBlock = extractSignatureBlock(msg.getPlainBody());
        if (sigBlock) {
          const phone = extractPhone(sigBlock);
          const senderEmail = findSenderEmail(msg.getFrom());
          if (phone && senderEmail && seen.has(senderEmail) && !seen.get(senderEmail).phone) {
            seen.get(senderEmail).phone = phone;
          }
        }
      }

      console.log(`  Thread "${subject}": ${seen.size} contact(s)`);
      for (const c of seen.values()) {
        console.log(`    - ${c.name || c.email} <${c.email}> · ${c.phone || 'no phone'} · ${c.company}`);
        if (!allContacts.has(c.email)) {
          allContacts.set(c.email, { contact: c, sources: [subject] });
        } else {
          allContacts.get(c.email).sources.push(subject);
          if (c.phone && !allContacts.get(c.email).contact.phone) {
            allContacts.get(c.email).contact.phone = c.phone;
          }
        }
      }
    }
  }

  console.log(`\n\n=== SUMMARY: ${allContacts.size} unique contact(s) ===`);
  for (const { contact, sources } of allContacts.values()) {
    console.log(`\n${contact.name || contact.email} <${contact.email}>`);
    console.log(`  Phone:   ${contact.phone || '—'}`);
    console.log(`  Company: ${contact.company}`);
    console.log(`  Seen in: ${sources.length} thread(s)`);
  }
}