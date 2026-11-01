# Demo video: script and shot list (up to 5 min, with a 3-minute cut)

For the team recording the Forge'26 demo. Record the live site, https://blindspot-web-five.vercel.app, at 1440 x 900
or full screen at 1920 x 1080, in a clean browser profile (no bookmarks bar, no extensions), with the system clock in
IST. Speak at about 130 words a minute. The full narration is about 620 words, 4 min 50 s.

**Length.** The submission rules (context.md, section 4) ask for a 2-3 minute video. The shots marked ★ make a
3-minute cut on their own: record the full version if the organisers have allowed 5 minutes, and
cut to the ★ shots if not. Never cut the investigator shot: it is what the rules ask the video to show ("real-time AI
reasoning, tool usage, or agentic execution under realistic conditions").

**Numbers change every hour.** Wherever the narration says *(read from screen)*, say the figure the page shows at the
time you record. Never say a number that is not on screen in that shot.

## Before you record

1. Warm the site: run `python submission/tools/warm_site.py` (one minute), or open `/`, `/live`, one case file,
   `/brief`, `/brief/patiala`, `/bulletin` and `/method`, each twice a minute apart. The first visit after a quiet
   spell wakes the API; a section that still says "took too long to answer" retries by itself within a few seconds.
2. In the same browser, visit every page you will show offline (shot 9) once while online, so they are saved.
3. On `/live`, allow the microphone once (click the microphone in the question box, then Allow), so the browser does
   not ask on camera. Ask at most one practice question: typed questions use the Gemini free tier. The questions
   under "Verified questions" replay stored, checked runs and cost nothing.
4. Prepare the two questions for shot 4. The first must be new, for example: "Which districts burned the most in
   the last 24 hours, and how much of the burnt area there had no fire alert?" The follow-up, spoken: "Which villages
   there should field teams check first?"
5. If you will show "Listen" in Hindi (shot 7), check that Windows has a Hindi voice: Settings > Time & language >
   Speech > Manage voices > add Hindi (India). For Punjabi most devices have no voice; the page then says so, which
   is fine to show.

## Shots

| # | Time | Screen | What you do | Narration |
| --- | --- | --- | --- | --- |
| 1 ★ | 0:00-0:20 | Landing, first screen | Point at the 24-hour dial: the gold marks, then the gap in the ring. | "India's official fire counts come from polar satellites that cross Punjab around 1:30, afternoon and night. Farmers burn later, around 4 to 5 PM. The gold marks are when a polar satellite actually reported a fire; the gap is the evening nobody counts. BlindSpot looks between the passes." |
| 2 ★ | 0:20-0:45 | Landing, the frames with scar outlines, then the large share | Let the newest frame show its outlines; scroll to the share. | "Sentinel-2 photographs every field every few days, and a burnt field stays dark whatever hour it was lit. We matched every burn scar against every fire alert between the passes: *(read from screen)* percent of the burnt area had no alert. We call these candidate unseen burns: a hand audit of 23 found 87 percent were real, so that share is an upper bound." |
| 3 ★ | 0:45-1:10 | Click **Open mission control**: `/live` | Hover the biggest district; point at the pass strip under the map. | "Mission control shows every district: the size is the area burnt, the colour how much of it had no fire alert. GK-2A, a geostationary satellite, scans every ten minutes: *(read from screen)* percent of its fire activity fell outside the polar satellites' windows." |
| 4 ★ | 1:10-2:15 | `/live`, the investigator | Type the prepared question and press Ask. Let the steps stream; do not cut. Point at the line under the answer. Click **Ask a follow-up**, click the microphone (**Speak your question**), say the follow-up, press Ask. When it answers, press **Listen** for two seconds, then stop it. | "Ask the investigator anything. Gemini plans the investigation and calls eleven tools on our live database: fires, scars, weather, smoke, field briefs. Watch the map follow it. Before you read the answer, every number in it is checked against what the tools returned, and every claim cites its source: *(read from screen)* figures checked. Follow up by voice, in English, Hindi or Punjabi: it keeps the thread, checks the new answer the same way, and can read it aloud." |
| 5 | 2:15-2:35 | A case file (click a row in the list, then **Open the case file**) | Show the before and after pictures, then scroll to the nearest fire alert. | "Every candidate has a case file: the two clear passes, why the rule calls it a burn, the nearest fire alert and how far away it was, and the wind between the passes." |
| 6 ★ | 2:35-3:05 | **Field brief** in the bar: `/brief`, then the top district | Open the district. Scroll to the villages. Click the Punjabi CSV button. Click **Print** and hold on the A4 preview, then cancel. | "For the pollution control boards' field teams, each district gets a brief: the villages with candidate unseen burns, a watchlist of harvested fields that rank highest for burning next, a spreadsheet in English, Hindi or Punjabi, and an A4 sheet to take to the field. **Ask the investigator** opens mission control with the district's question ready." |
| 7 | 3:05-3:25 | **Bulletin** in the bar | Switch English, Hindi, Punjabi. Press **Listen** on the Hindi bulletin for three seconds. | "Every morning a bulletin in English, Hindi and Punjabi, written by Gemini under the same number checks, and read aloud on any device with the voice." |
| 8 | 3:25-3:55 | **Method** in the bar, the section "How the investigator is checked" | Hold on the record and the diagram of the checks; then scroll to the validation table. | "We publish how often each part is right, including where it is weak. The investigator's own record, read live: *(read from screen)*. The smoke index and the burn-risk ranking are marked as not yet robust, with their numbers." |
| 9 | 3:55-4:30 | Any page, then the theme switch, then offline | Switch to the light theme with the sun/moon button in the bar and back. Turn Wi-Fi off. Open `/live` and `/brief/patiala` again: point at the notice at the bottom. Open a page you never visited: the offline screen lists the saved pages. Turn Wi-Fi on: the page refreshes by itself. | "Built for a live demo on venue Wi-Fi: dark or light, and if the connection drops, every page you have opened keeps working from the last data this browser saw, and says how old it is. When it comes back, the page refreshes by itself." |
| 10 ★ | 4:30-4:50 | Back to the landing first screen | Hold. | "Everything is live data from NASA, ESA, the Korean weather satellite and India's own monitors, refreshed by scheduled jobs and served on free tiers. The code is open. BlindSpot: look between the passes." |

The 3-minute cut is shots 1, 2, 3, 4, 6 and 10: about 3 minutes. If it runs over, drop the Print preview from shot 6
and the Listen moment from shot 4.

## After recording

- Check that every number you said matches the frame it was said over.
- Check that nothing private is visible (other tabs, notifications, the address bar's suggestions).
- If the investigator says the daily free budget is used up, click one of the "Verified questions" instead and say
  that it replays a stored, checked run. Listen works on a replay too; a follow-up needs an answer from the last 24
  hours, so skip it in that case.
