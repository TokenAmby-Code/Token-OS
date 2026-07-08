#!/usr/bin/env node
// daemon.js — launchd entry shim (ai.tokenclaw.discord invokes this stable
// path). The daemon body lives in daemon-main.ts, executed via Node 22
// native type-stripping; keeping the .js entry means the plist never changes.
import './daemon-main.ts';
