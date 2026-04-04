// transcribe.js — Audio transcription pipeline
// Supports: Wispr Flow (via Hammerspoon bridge), OpenAI Whisper API, local whisper.cpp
//
// Default provider: wispr (zero cost, uses Emperor's formatting rules)
// Fallback: openai (requires API key)

import { execFile } from 'child_process';
import { writeFileSync, readFileSync, unlinkSync, existsSync } from 'fs';
import { join } from 'path';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);

/**
 * Convert raw PCM (48kHz mono s16le) to 16kHz mono WAV for Whisper API
 */
async function pcmToWav(pcmBuffer, outputPath) {
  const inputPath = outputPath.replace('.wav', '.pcm');
  writeFileSync(inputPath, pcmBuffer);

  await execFileAsync('/opt/homebrew/bin/ffmpeg', [
    '-y',
    '-f', 's16le',
    '-ar', '48000',
    '-ac', '1',
    '-i', inputPath,
    '-ar', '16000',
    '-ac', '1',
    outputPath,
  ]);

  try { unlinkSync(inputPath); } catch {}
  return outputPath;
}

/**
 * Transcribe via Wispr Flow (Hammerspoon bridge on :7780)
 * Flow: send PCM path → Hammerspoon plays through BlackHole → Wispr transcribes → paste captured
 */
async function transcribeWispr(pcmPath, logger) {
  const BRIDGE_URL = 'http://127.0.0.1:7780';

  // Check bridge is up
  try {
    const statusResp = await fetch(`${BRIDGE_URL}/status`);
    if (!statusResp.ok) throw new Error('Bridge not responding');
    const status = await statusResp.json();
    if (status.transcribing) {
      logger.warn('Transcriber: Wispr bridge busy, queuing...');
      // Wait up to 30s for it to finish
      for (let i = 0; i < 30; i++) {
        await new Promise(r => setTimeout(r, 1000));
        const s = await (await fetch(`${BRIDGE_URL}/status`)).json();
        if (!s.transcribing) break;
      }
    }
  } catch {
    throw new Error('Wispr bridge not running (Hammerspoon :7780)');
  }

  // Submit transcription job
  const resp = await fetch(`${BRIDGE_URL}/transcribe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ audio_path: pcmPath }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Wispr bridge error (${resp.status}): ${err}`);
  }

  const job = await resp.json();
  if (!job.job_id) throw new Error('No job_id returned from bridge');

  // Poll for result (Wispr transcription is async — playback + paste + read)
  const maxWaitMs = 30000;
  const pollInterval = 500;
  const start = Date.now();

  while (Date.now() - start < maxWaitMs) {
    await new Promise(r => setTimeout(r, pollInterval));

    const resultResp = await fetch(`${BRIDGE_URL}/result/${job.job_id}`);
    const result = await resultResp.json();

    if (result.text !== undefined) {
      return result.text || null;
    }
    // Still processing — continue polling
  }

  throw new Error('Wispr transcription timed out');
}

/**
 * Transcribe audio using OpenAI Whisper API
 */
async function transcribeWhisperAPI(wavPath, apiKey, options = {}) {
  const formData = new FormData();

  const wavBuffer = readFileSync(wavPath);
  const blob = new Blob([wavBuffer], { type: 'audio/wav' });
  formData.append('file', blob, 'audio.wav');
  formData.append('model', options.model || 'whisper-1');
  formData.append('language', options.language || 'en');
  if (options.prompt) formData.append('prompt', options.prompt);

  const resp = await fetch('https://api.openai.com/v1/audio/transcriptions', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${apiKey}` },
    body: formData,
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Whisper API error (${resp.status}): ${err}`);
  }

  const result = await resp.json();
  return result.text;
}

/**
 * Create a transcription handler for the voice manager
 */
export function createTranscriber(config, logger) {
  const provider = config.whisper_provider || 'wispr'; // 'wispr' | 'openai' | 'local'
  const apiKey = config.openai_api_key || process.env.OPENAI_API_KEY;
  const audioDir = join(process.env.HOME || '/tmp', '.discord-cli', 'audio');

  logger.info(`Transcriber: provider=${provider}`);

  if (provider === 'openai' && !apiKey) {
    logger.warn('Transcriber: No OpenAI API key — openai provider disabled');
  }

  // Transcription result callbacks
  const resultHandlers = [];

  async function handleAudio(userId, pcmBuffer, pcmPath, botName) {
    const timestamp = Date.now();

    try {
      let text;

      if (provider === 'wispr') {
        // Wispr Flow uses the raw PCM file directly — Hammerspoon handles playback
        text = await transcribeWispr(pcmPath, logger);
      } else if (provider === 'openai') {
        if (!apiKey) {
          logger.warn('Transcriber: skipping — no API key');
          return;
        }
        const wavPath = join(audioDir, `${userId}-${timestamp}.wav`);
        await pcmToWav(pcmBuffer, wavPath);
        text = await transcribeWhisperAPI(wavPath, apiKey);
        if (!config.keep_audio) { try { unlinkSync(wavPath); } catch {} }
      }

      if (!text || text.trim().length === 0) {
        logger.debug('Transcriber: empty transcription, skipping');
        return;
      }

      logger.info(`Transcriber [${botName || 'unknown'}]: [${userId}] "${text}"`);

      // Notify handlers
      const result = { userId, text, timestamp, pcmPath, botName };
      for (const handler of resultHandlers) {
        try {
          await handler(result);
        } catch (err) {
          logger.error(`Transcriber: handler error: ${err.message}`);
        }
      }

      return text;
    } catch (err) {
      logger.error(`Transcriber: failed for ${userId}: ${err.message}`);
      return null;
    }
  }

  return {
    handleAudio,
    onTranscription(handler) { resultHandlers.push(handler); },
  };
}
