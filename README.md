# ReelCast Lab

Record a video on your phone, drop it in a folder on your Mac, and get back five edited versions and a written report.

A small background job notices the new file. It transcribes it on your Mac, asks Claude to plan the edit, renders five versions that each change one thing, runs a quality check, and then waits for you to look at them. Nothing is posted anywhere.

## What you get for each video

| Version | What changes |
|---|---|
| A, control | Your story as you told it, with retakes, filler words and long pauses cut |
| B, on-screen hook | The same cut, with two short lines of text on screen for the first 3 seconds |
| C, cold open | The strongest line moved to the very front |
| D, tight cut | A faster, shorter telling (about 60 percent of A) |
| E, visual open | The same cut, opening on a close zoom that eases out |

All five have word by word captions, your end card, and sound set to the loudness Instagram expects. Beside them you get `REPORT.md` (what was cut, every judgment call, anything that went wrong) and `caption.txt` (a post caption). A real report is in [examples/first-video-report.md](examples/first-video-report.md).

## What works today and what does not

Works, and has been run on real videos:

- Drop a file, transcribe it offline, edit, render, quality check, hold for your review
- A retry command for a video that got stuck

In the code but switched off, coming later:

- Posting the versions for you
- Reading the view numbers back
- Picking a winner and a results page

You post the versions yourself for now.

## What you need

- A Mac with Apple silicon (M1 or newer) and about 10 GB of free space
- [Homebrew](https://brew.sh)
- [Claude Code](https://claude.com/claude-code), signed in on a paid Claude plan
- Video filmed upright on a phone, one person talking to the camera, with a finished cut under 4 minutes

Plan for the cost. Each video is one long Claude session. The first real video through this system was 88 seconds long and its edit took about 50 minutes. On a smaller Claude plan that can use most of a usage window.

## Set it up

1. Get this folder onto your Mac: `git clone <the link from your email>`
2. Open the folder in Claude Code.
3. Say: **set me up**

Claude walks you through the rest one question at a time: installing the tools, your name and look, a practice run on the sample clip, and your first real video. The guide it follows is [SETUP.md](SETUP.md). The first run downloads a speech model (about 3 GB), once.

## Using it

1. Drag a video into `RAW/Singles/` inside your ReelCast Lab folder. For several clips that make one video, put them together in one folder inside `RAW/Multi-part/`.
2. Wait. The file is picked up 10 to 20 minutes after it stops changing, and the edit can take up to an hour.
3. Open `Edited/<date>_<title>/`. Watch the five versions and read the report.
4. Your original file is kept, untouched, in `Originals/`.

Anything the system cannot handle goes to `RAW/Needs-attention/` with a note saying why.

Useful commands, run from this folder:

```
python3 engine/tick.py status          what each video is doing
python3 engine/tick.py doctor          check that everything is installed
python3 engine/tick.py retry RUN_ID    send a stuck video back to the step that failed
python3 engine/dryrun.py FOLDER        practice run on the sample clip, no Claude used
```

To pause everything, create an empty file named `PAUSED` in `Lab_Data/`.

## What the editor can and cannot do on your Mac

The edit is done by Claude running without a window. It is boxed in:

- It can write only in that video's work folder and its output folder.
- It cannot reach the internet.
- It cannot change your originals, this kit, or anything else on your Mac.
- It loads none of your Claude connectors.

Your video is transcribed on your Mac. Claude sees the transcript and a few still frames from the video, through your own Claude account. The video file itself is not uploaded by this kit.

## Remove it

```
python3 scripts/install_job.py --uninstall
```

That prints the one command that stops the background job. Then delete this folder. Your videos stay where they are.

## Credits

- Built on [Vibe Editing](https://github.com/maddexritter-rgb/vibe-editing) (MIT). Setup downloads a fixed version of it. Its offline transcription script and editing playbooks are used as they are.
- Captions use the Inter typeface (SIL Open Font License).
- Speech to text is OpenAI's Whisper model, run locally through faster-whisper.

See [NOTICE](NOTICE) for the details.

## License

MIT, see [LICENSE](LICENSE). The files in `examples/` and `sample/` are there to look at and test with. They are not covered by the license and may not be reused.
