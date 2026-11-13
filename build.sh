#!/bin/sh
# Rebuild spread-bot.mjs from src/
cd "$(dirname "$0")"
{ echo "import fs from 'node:fs';"; cat src/core.js src/desk.js src/notes.js src/policy_v2.js src/policy_v4.js; grep -v "^import fs" src/main.js; } > spread-bot.mjs
node --check spread-bot.mjs && echo "built spread-bot.mjs"
