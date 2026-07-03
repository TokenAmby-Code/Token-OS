// outbound-message.js — deterministic Discord content chunking.
//
// Discord rejects message content above 2000 UTF-16 code units. Keep this logic
// local and deterministic so CLI/HTTP/recovery paths can prove they never hand a
// provider send a too-large content body.

export const DISCORD_MESSAGE_CONTENT_LIMIT = 2000;

function isHighSurrogate(code) {
  return code >= 0xd800 && code <= 0xdbff;
}

function safeCutIndex(text, index) {
  if (index <= 0 || index >= text.length) return index;
  return isHighSurrogate(text.charCodeAt(index - 1)) ? index - 1 : index;
}

function findBestCut(text, limit) {
  let inFence = false;
  let lastNewlineOutside = -1;
  let lastWhitespaceOutside = -1;
  let lastNewlineAnywhere = -1;
  let lastWhitespaceAnywhere = -1;

  for (let i = 0; i < limit; i += 1) {
    if (text.startsWith('```', i)) {
      inFence = !inFence;
      i += 2;
      continue;
    }

    const ch = text[i];
    const next = i + 1;
    if (ch === '\n') {
      lastNewlineAnywhere = next;
      if (!inFence) lastNewlineOutside = next;
    } else if (/\s/.test(ch)) {
      lastWhitespaceAnywhere = next;
      if (!inFence) lastWhitespaceOutside = next;
    }
  }

  // Prefer line boundaries, then word boundaries, and prefer boundaries outside
  // code fences. If the only boundary is inside a very large fence, use it rather
  // than exceeding the provider limit.
  const cut =
    lastNewlineOutside > 0 ? lastNewlineOutside
      : lastWhitespaceOutside > 0 ? lastWhitespaceOutside
        : lastNewlineAnywhere > 0 ? lastNewlineAnywhere
          : lastWhitespaceAnywhere > 0 ? lastWhitespaceAnywhere
            : limit;
  return safeCutIndex(text, cut) || safeCutIndex(text, limit);
}

export function splitDiscordMessageContent(content, limit = DISCORD_MESSAGE_CONTENT_LIMIT) {
  if (typeof content !== 'string') return [];
  if (limit <= 0) throw new Error('Discord message chunk limit must be positive');
  if (content.length <= limit) return [content];

  const chunks = [];
  let rest = content;
  while (rest.length > limit) {
    const cut = findBestCut(rest, limit);
    chunks.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest.length > 0 || chunks.length === 0) chunks.push(rest);
  return chunks;
}

function firstMessageId(result) {
  return result?.message_id || result?.id || null;
}

export function summarizeChunkedDiscordSend(sent, chunks, totalLength) {
  const first = sent[0] || {};
  return {
    message_id: firstMessageId(first),
    channel_id: first.channel_id || first.channelId || null,
    timestamp: first.timestamp || null,
    chunked: true,
    chunk_count: chunks.length,
    total_length: totalLength,
    max_chunk_length: Math.max(...chunks.map(c => c.length)),
    message_ids: sent.map(firstMessageId).filter(Boolean),
    messages: sent,
  };
}

export async function sendChunkedDiscordContent(
  content,
  sendChunk,
  {
    limit = DISCORD_MESSAGE_CONTENT_LIMIT,
    firstOptions = {},
    subsequentOptions = {},
  } = {},
) {
  if (typeof content !== 'string') {
    return sendChunk(content, {
      index: 0,
      count: 1,
      is_first: true,
      is_last: true,
      chunk_length: 0,
      total_length: 0,
      options: firstOptions,
    });
  }

  const chunks = splitDiscordMessageContent(content, limit);
  const sent = [];

  for (let index = 0; index < chunks.length; index += 1) {
    const chunk = chunks[index];
    const isFirst = index === 0;
    const options = isFirst ? firstOptions : subsequentOptions;
    const result = await sendChunk(chunk, {
      index,
      count: chunks.length,
      is_first: isFirst,
      is_last: index === chunks.length - 1,
      chunk_length: chunk.length,
      total_length: content.length,
      options,
    });
    sent.push(result);
  }

  if (chunks.length === 1) return sent[0];
  return summarizeChunkedDiscordSend(sent, chunks, content.length);
}

export function isDiscordContentLengthValidationError(error) {
  const message = String(error?.message || error || '');
  return (
    message.includes('content[BASE_TYPE_MAX_LENGTH]')
    || /Must be 2000 or fewer in length/i.test(message)
  );
}
