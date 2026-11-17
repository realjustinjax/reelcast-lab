# Setup guide (Claude follows this, one step at a time)

This guide is written for Claude Code to walk a person through. The person is a creator, not a developer. Do each step for them, say what you are doing in plain words, ask one question at a time, and wait for a yes before anything that installs software or starts the background job. Run every command from the root of this folder.

Start with `python3 scripts/setup_env.py check` and skip any step that is already done.

## Step 1. Check the Mac

- `uname -m` must print `arm64` (Apple silicon). On an Intel Mac, stop and say this kit has only been built and tested on Apple silicon.
- Check there is about 10 GB free (`df -h ~`).
- Check Homebrew: `brew --version`. If it is missing, send them to https://brew.sh, ask them to follow the one install line there themselves, and continue when they say it is done.

## Step 2. Install the tools

Tell them what will be installed and why, then with their yes run:

```
brew install ffmpeg-full tesseract python@3.12
```

- `ffmpeg-full` cuts the video and burns in the captions. The plain `ffmpeg` cannot burn captions, so the full one is needed even if they already have ffmpeg. It installs beside the other and does not replace it.
- `tesseract` reads text off finished frames for the quality check.
- `python@3.12` runs the renderer and the speech to text.

## Step 3. Build the kit's own workspace

```
python3 scripts/setup_env.py install
```

This creates a private Python workspace in `.venv/`, installs fixed versions of the packages, and downloads one fixed version of Vibe Editing into `vendor/`. It takes a few minutes. If it stops with a message, do what the message says and run it again.

Never run anything inside `vendor/vibe-editing` yourself: not its setup script, not its onboarding, not `editorial_brain.py`.

## Step 4. Check Claude Code

- `claude --version` must work in the terminal.
- Ask: "Are you signed in to Claude Code with a paid Claude plan?" Each video is one long editing session (the first real video took about 50 minutes), so a smaller plan may only manage one video in a usage window. Say that plainly.

## Step 5. Where the videos live

Ask: "Where should your ReelCast Lab folder live? The default is a folder named ReelCast Lab in your home folder." Then save it and create the folders:

```
python3 scripts/setup_env.py install --settings-json '{"content_root": "~/ReelCast Lab"}'
```

Show them the folder in Finder (`open "<the folder>"`). Explain the four folders: `RAW` (they drop videos here), `Edited` (finished versions and reports), `Originals` (their untouched files), `Lab_Data` (the system's own notes, they can ignore it).

## Step 6. Their name and look (one question at a time)

Save answers as you go with `python3 scripts/setup_env.py install --settings-json '{...}'`. Brand files go in `Lab_Data/brand/` inside their ReelCast Lab folder. Anything they skip keeps the neutral default.

1. "What name should the editor use for you?" Save as `{"owner": {"name": "..."}}`.
2. "What colour should the key words in your captions be? Give a colour name or a hex code." Copy `brand/default/default_pop.json` to `Lab_Data/brand/caption_preset.json` and change only the key word colour. Leave sizes and positions alone.
3. "How do you want to sound in post captions? Two or three sentences, and any words you never use." Write it to `Lab_Data/brand/voice.md`.
4. "Are there names or words that speech to text gets wrong for you, like a product name?" Add each to the `glossary` setting. The format is described in `engine/config.json`.
5. "Is there anything at the top of your shot you never want to show?" If yes, note it in `Lab_Data/brand/framing.md`. Leave `framing.top_crop` at 0 until they have seen a first video.
6. "Do you want an end card with your name on it at the end of each video?" If yes, ask for their name, a one line title, and optionally a square photo, then build it with `brand/end-card/build_end_card.py` (see its `--help`) into `Lab_Data/brand/end-card.png`. If the builder cannot make the picture on this Mac, keep the default card and say so.

## Step 7. Check everything

```
python3 engine/tick.py doctor
```

Every line must pass. Fix what it names before going on.

## Step 8. Practice run (no Claude used, nothing posted)

```
python3 engine/dryrun.py ~/reelcast-lab-practice
```

This runs the sample clip through transcription, rendering and the quality check in a scratch folder. The first run downloads the speech model (about 3 GB), so it can take a while. When it finishes it prints a folder. Open it and show them the five versions. The cut in the practice run is made by a simple stand-in, not by Claude, so it is rough on purpose. It proves the install works.

Do not continue until the practice run has produced five versions.

## Step 9. First real video

1. Ask them to drag one upright phone video (under about 5 minutes of raw footage) into `RAW/Singles/`.
2. Run `python3 engine/tick.py` now, and again every few minutes, or go to step 10 first so it runs by itself. The file is picked up once its size has stopped changing between two checks about 10 minutes apart.
3. `python3 engine/tick.py status` shows the step it is on. The edit can take up to an hour.
4. When the status says it is waiting for review, open the folder in `Edited/` and show them the five versions and `REPORT.md`.
5. If they are happy: `python3 engine/tick.py approve RUN_ID`. Nothing is posted. Approving only marks the video as reviewed.

If it lands in `RAW/Needs-attention/`, read the note there, fix the cause, and run `python3 engine/tick.py retry RUN_ID`.

## Step 10. Let it run by itself

Ask first. Then:

```
python3 scripts/install_job.py
```

It prints the commands that start a small background job, which checks the folder every 10 minutes. Run those commands with their yes. From then on they only drag videos into `RAW/Singles/`.

To stop it later: `python3 scripts/install_job.py --uninstall` prints the command.

## What to tell them at the end

- Where to drop videos, and where finished ones appear.
- That posting, view numbers and picking a winner are not finished yet, so they post the versions themselves.
- That their originals are kept in `Originals/` and are never changed.
