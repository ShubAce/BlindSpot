# Demo video: script and shot list (2 min 45 s)

For the team recording the Forge'26 demo. Record the live site, https://blindspot-web-five.vercel.app, at 1440 x 900
in a clean browser profile (no bookmarks bar, no extensions), with the system clock in IST. Speak at about 130 words a
minute; the narration below is about 340 words.

**Numbers change every hour.** Wherever the narration says *(read from screen)*, say the figure the page shows at the
time you record. Never say a number that is not on screen in that shot.

## Before you record

1. Open these tabs: the landing page, `/live`, one case file (click the top row of "Candidate unseen burns" on `/live`),
   `/bulletin`, `/method`. Load each one twice, a minute apart: right after a deploy the first visit wakes the API and
   can show "took too long to answer" in a section; the second is served from the cache.
2. On `/live`, ask one practice question so the page is warm. Typed questions use the Gemini free tier, so ask at most
   two or three while rehearsing. The example questions replay stored runs and cost nothing.
3. Prepare the live question for shot 4. It must be new (not one of the examples), for example:
   "Which districts burned the most in the last 24 hours, and how much of the burnt area there had no fire alert?"

## Shots

| Time | Screen | What you do | Narration |
| --- | --- | --- | --- |
| 0:00-0:15 | Landing, first screen | Point at the 24-hour dial: the gold marks, then the gap in the ring. | "The fire satellites behind India's counts cross Punjab around 1:30, afternoon and night. The burning peaks later, around 4 to 5 PM. From *(read from screen: the times in the dial)*, no fire satellite looks at all. BlindSpot counts the fires they miss." |
| 0:15-0:40 | Landing, frames with scar outlines | The frame opens on the newest date with its outlines; click an older date, then the newest again. Scroll to the large share. | "Sentinel-2 photographs every field every few days, and a burnt field stays dark whatever hour it was lit. We matched every scar against every fire alert between the passes. *(read from screen)* percent of the burnt area had no alert. We call these candidate unseen burns: a hand audit of 23 found 87 percent were real, so that share is an upper bound." |
| 0:40-1:05 | `/live` | Hover the biggest district hexagon, then turn on Satellites and hover the pass strip. | "Mission control shows every district: the size is the area burnt, the colour how much of it had no fire alert. Below, who was watching Punjab, and when the next fire satellite comes. GK-2A, a geostationary satellite, scans every ten minutes: *(read from screen)* percent of its fire activity fell outside the polar satellites' windows." |
| 1:05-1:50 | `/live`, investigator | Type the prepared question and press Ask. Let the steps stream; do not cut. | "Ask the investigator anything. Gemini plans the investigation and calls our tools on the live database: fires, scars, weather, smoke. Watch the map follow it. Before you see the answer, every number in it is checked against what the tools returned, and every claim cites its source." Then, over the verification line: "*(read from screen)* of *(read from screen)* figures checked." |
| 1:50-2:10 | Case file | Show the before and after pictures, then scroll to the rule and the nearest fire alert. | "Every candidate has a case file: the two clear passes, why the rule calls it a burn, the nearest fire alert and how far it was, and the wind between the passes." |
| 2:10-2:25 | `/bulletin`, then `/method` | Switch English, Hindi, Punjabi. On `/method`, show the blind-hours chart. | "A daily bulletin in English, Hindi and Punjabi, checked number by number. The method page shows how often each part is right, read live." |
| 2:25-2:45 | `/method`, "How the parts connect" | Hold on the architecture diagram, then return to the landing. | "Everything is live data from NASA, ESA, the Korean weather satellite and India's own monitors, refreshed by scheduled jobs and served on free tiers. The code is open. BlindSpot: look between the passes." |

## After recording

- Check that every number you said matches the frame it was said over.
- Keep it under 3 minutes. If you must cut, shorten shot 3, never shot 4: the live investigator is the part judges
  remember.
