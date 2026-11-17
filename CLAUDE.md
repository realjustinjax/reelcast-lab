# ReelCast Lab: how to help in this folder

This folder is ReelCast Lab. It turns a raw phone video into five edited versions and a written report. The people who use it are creators, not developers.

## How to work with the person

- Do the work yourself. Run the commands for them and explain what is happening in plain words.
- Ask one question at a time and wait for the answer.
- Never ask for a password, key or token. This kit needs none.
- Before anything that installs software or starts the background job, say what it will do and wait for a yes.

## When they say "set me up"

Follow [SETUP.md](SETUP.md) from the top, step by step. Run `python3 scripts/setup_env.py check` first to see what is already done, and skip finished steps.

## When something is wrong

1. `python3 engine/tick.py doctor` says what is missing.
2. `python3 engine/tick.py status` says what each video is doing.
3. The log is at `~/Library/Logs/reelcast-lab.log`.
4. A video in `RAW/Needs-attention/` has a note saying why. After fixing the cause, `python3 engine/tick.py retry RUN_ID` sends it back to the step that failed.

## Rules

- Nothing in this kit posts, uploads or sends. Posting, reading numbers and picking a winner are switched off and are not finished. Do not switch them on and do not promise them.
- Never delete or change a file in `Originals/` or `RAW/`.
- Never run anything from `vendor/vibe-editing` except through this kit. Never run its setup, its onboarding or `editorial_brain.py`.
- Do not edit the commands in `engine/config.json` that start Claude. They keep `--strict-mcp-config`, the sandbox setting and the deny rules for a reason. A person's own choices go in their settings file through `python3 scripts/setup_env.py --settings-json`.
- A person's look, voice notes and end card live in `Lab_Data/brand/` inside their ReelCast Lab folder, never in this folder.
