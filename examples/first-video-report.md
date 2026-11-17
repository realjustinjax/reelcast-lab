# Example report: five test versions of one video

This is the report the editor wrote for the first real video that went through the system. Personal details have been changed or removed, and the spoken lines are summarized instead of quoted. The structure, the checks and the problems are as they happened.

---

This folder holds five edits of the same raw video (about 88 seconds of you talking to the phone, handheld). Each version changes one thing against version A, so you can test which one does best. All five use the same captions, the same end card and the same post caption (`caption.txt`).

The story: founders work weekends, and here are two things I finished. One, a content system (record it, drop it in a folder, and five edited versions come back). Two, an old computer was retired and its jobs moved to a newer one. It ends on your spoken ask to comment a keyword to hear how it was built.

## The five versions

| Version | File | Length with end card | Length before end card | What is different from A |
|---|---|---|---|---|
| A | `A_control.mp4` | 1:10 (69.5s) | 67.5s | Nothing. The story as you told it, both weekend wins, cleaned up. |
| B | `B_onscreen_hook.mp4` | 1:10 (69.5s) | 67.5s | For the first 3 seconds, two lines sit just below your mouth: "You just hit record" / "Claude cuts 5 versions" ("record" and "5" in the key colour). |
| C | `C_cold_open.mp4` | 1:09 (68.6s) | 66.6s | Opens on the line about recording a video, dropping it in a folder and having it edited. Then the story starts from the beginning. |
| D | `D_tight_cut.mp4` | 0:42 (42.4s) | 40.4s | A faster, shorter version of the same story (about 60 percent of A). |
| E | `E_visual_open.mp4` | 1:10 (69.5s) | 67.5s | The first 3 seconds start zoomed in close (1.2x) and ease back out to the normal frame. No extra text. |

Every file ends with your end card, held for 2 seconds in silence, fading in over 0.2 seconds.

## What A keeps

1. The opening about working weekends and the promise of two wins.
2. The first win: the content system, in your words.
3. The proof line: a six minute raw video came back as five short versions.
4. The second win: the old computer is retired and everything now runs on the new one.
5. The closing ask to comment a keyword.

Cut out: the first try at the "first" section (you turned the camera away and started again), several false starts, a doubled word, two "uh"s, a couple of loose "and so" starters, and the long pauses.

## What D drops to get to about 40 seconds

It opens straight on the promise of two wins. Gone: the line about weekends coming with the territory, the false start before the first win, the proof line, a side remark, and most of the detail in the second win. It keeps the punch line of the second win and the closing ask.

## The look

- **Framing:** the phone is handheld, close and low, and it moves. There was something at the top of the shot you asked never to show, and its edge slopes in this recording. I set the top crop at 22 percent. Going higher would have cut off the top of your head in several shots.
- **Punch-ins (10%)** on key lines only: the line about the edit being done for you, the "five versions" line, the retirement line, and the closing ask.
- **Captions:** word by word, bold, white with a few key words per line in your key colour, placed on your chest below your mouth. Normal sentence case.
- **Sound:** your voice only, no music, no sound effects, set to Instagram loudness (-14 LUFS).

## Checks I ran on every file

| Check | A | B | C | D | E |
|---|---|---|---|---|---|
| Size | 145 MB | 145 MB | 143 MB | 85 MB | 145 MB |
| Video | H.264 High, 1080x1920, 30 fps, SDR BT.709 | same | same | same | same |
| Audio | AAC stereo 48 kHz | same | same | same | same |
| Loudness | -14.0 LUFS | -14.0 | -14.1 | -14.0 | -14.0 |
| Peak | -1.5 dB | -1.5 dB | -1.5 dB | -1.4 dB | -1.5 dB |
| Speech starts at | under 0.05s | under 0.05s | 0.10s | under 0.05s | under 0.05s |
| End card silent | yes | yes | yes | yes | yes |

- I ran a fresh transcription of each finished file and read it through. Each one opens on the right line, with no clipped words, no doubled phrases, and no "um" or "uh".
- I pulled frames from the start, middle and end card of each file, plus six frames from B's first 3 seconds: captions and the hook stay below your mouth, the end card is there, E opens zoomed in.

## Things I decided that you may want to change

- **A misheard product name.** Speech to text wrote one product name where another similar sounding one was possible. In both places you meant the first, so the captions keep it.
- **"toward" is "to where".** The transcript had one, the audio says the other. I checked that bit of audio again and fixed the caption.
- **The spoken ask is a keyword ask.** I kept it in all five because it is your call to action as you said it. The post caption ends on a save and follow ask instead. If there is no automatic reply set up for that keyword, people who comment will be waiting on you by hand.
- **The B hook** speaks to "you" and stays specific. In every frame of the first 3 seconds that I checked, the top of the hook stays below your lower lip, and there is clear space between the hook and the captions.
- **C's cold open** is moved, not copied, so the line does not play again later.

## Second pass: B's hook and the quality check

The quality check sent B back because it could not read the hook. The hook was on screen and readable the whole time. The problem was in the check: it read both hook lines as one strip of text and garbled them. I moved the hook up slightly so the check could read it, re-rendered B only, and it passed. A, C, D and E were not touched.

(This check has since been fixed. It now reads each hook line on its own.)

## What didn't work or wasn't run

- **Word timings were off by 0.2 to 0.5 seconds in the second half of this video.** My first renders clipped a few words and let removed words leak back in. I found each one in the read-back, tested the exact cut points on short pieces of the raw audio, set the cuts by hand, and confirmed the fixes on the finished files.
- **A renderer quirk:** when two kept pieces sat next to each other, the renderer ignored a hand-set cut point between them. I worked around it in the plan. (Also fixed since: a hand-set cut point now always wins.)

## What I would fix with more time

1. **One cut lands right on your voice** at about 0:46 in A. It reads clean in the re-checks, but listen there for a tiny click.
2. **Jump cuts.** It is handheld, so most cuts hide in the movement. The jumpiest stretch is 0:42 to 0:50 in A, where several small pieces sit close together.
3. **Try B's hook on top of D**, since D is short enough that most people will watch it to the end.
4. **Files are big (145 MB for 70 seconds)** because handheld footage is hard to compress. Instagram accepts it, but a lighter encode would upload faster.
