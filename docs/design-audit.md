# Design audit: Multi-MP3 web UI

## After the redesign (all 24 items applied)

| | Before | After |
|---|---|---|
| Impeccable detector | 10 findings | **0** |
| Impeccable health score | 9 / 20 (*Poor*) | **19 / 20** (*Excellent*): accessibility 4, performance 4, theming 4, responsive 3, integrity 4 |
| Lighthouse, mobile | not run | **Accessibility 100 · Best practices 100 · SEO 100** |
| Hallmark (re-checked by hand against its list of tells) | 2 critical · 9 major · 4 minor | **0 · 0 · 0**, none of the listed tells remain |

**What changed:**
- **Colours:** its own scheme (warm graphite plus one ice-cyan accent), with a light theme that follows the system setting. Every colour pair was checked at 4.5:1 or better in both themes.
- **Type:** self-hosted Bricolage Grotesque for headings and controls, plus JetBrains Mono for numbers.
- **Icons:** one SVG icon set instead of emoji.
- **Layout:** flat rows instead of boxes inside boxes.
- **Accessibility:** keyboard-operable expanders and tabs, a live region for status changes, failure reasons shown as text, 44 px touch targets, and support for reduced motion.
- **Playlist page:** one shared player with play all, shuffle and next/previous.

Item-by-item:

1. **Fixed.** The provider header wraps cleanly on phones; nothing is clipped.
2. **Fixed.** Expanders are buttons with `aria-expanded`.
3. **Fixed.** Failure reasons are shown under each track.
4. **Fixed.** Buttons have dark text on the accent colour (10.8:1).
5. **Fixed.** Only the track in flight animates; reduced motion is respected.
6. **Fixed.** Status changes are announced.
7. **Fixed.** The tabs use proper tab markup with arrow keys.
8. **Fixed.** Controls are 44 px on touch screens.
9. **Fixed.** No side stripes.
10. **Fixed.** No nested boxes.
11. **Fixed.** One SVG icon set.
12. **Fixed.** No glow shadows.
13. **Fixed.** No gradients.
14. **Fixed.** No hover movement.
15. **Fixed.** Transitions name their properties.
16. **Fixed.** One set of rules, all colours in variables.
17. **Fixed.** The app's own colour scheme.
18. **Fixed.** The count stays; the % inside the bar is gone.
19. **Fixed.** A small log icon per playlist; the run log is collapsed.
20. **Fixed.** Real ellipses.
21. **Fixed.** Consistent alignment.
22. **Fixed.** Underlined tab strip.
23. **Fixed.** No spaced-out capitals.
24. **Fixed.** One player on the playlist page.

Responsive scores 3 rather than 4 only because touch *gestures* weren't exercised on a real device: layout was checked at 390 px in Chrome's emulation.

The original audit follows, unchanged.

---

Audited on 10 Oct 2026. Covers `templates/index.html`, `templates/playlist.html` and `static/style.css`.

**How it was done**
- **Two skills:** the Impeccable `audit` (scores five technical areas and runs its own detector over the code) and Hallmark's `audit` (looks for named "AI-generated UI" tells).
- **Real UI:** I looked at the running app in Chrome at 1280 px and at phone width (390 px). The Live tab was filled by replaying a fake mid-run, so nothing was downloaded.
- **Report only:** nothing here has been changed yet. Pick the items you want and I'll apply them.

Hallmark is built mainly for marketing pages, so its rules about hero sections, navigation bars and footers don't apply to an app like this and were skipped. Impeccable's `init` (which writes a product brief, `PRODUCT.md`) wasn't run, because it needs a short interview with you. The audit doesn't need it, but a redesign would.

---

## Verdict

| | Result |
|---|---|
| Impeccable health score | **9 / 20**, rated *Poor* (6–9 means "major overhaul") |
| Impeccable detector | 10 findings: 6 coloured side-stripes on cards, 2 glow shadows, 2 text-contrast failures |
| Hallmark | **2 critical · 9 major · 4 minor**, verdict *reads as AI-generated* |

The code behind the page is good. Updating only the parts that change keeps the Live tab smooth with thousands of tracks. The look is the problem. It copies GitHub's dark colour scheme exactly, so it has no identity of its own. On top of that it uses nearly every decoration that marks a UI as AI-generated: coloured stripes down the side of cards, gradients on buttons and progress bars, coloured glow shadows, cards that lift on hover, things pulsing constantly, and emoji in place of icons. The two most important problems aren't visual, though: on a phone, the provider headers cut off their own status, and some of the controls can't be used from a keyboard.

### Impeccable scores

| # | Area | Score | Main finding |
|---|---|---|---|
| 1 | Accessibility | 1 | The expandable provider and playlist headers are plain `<div>`s that keyboard users can't reach. White text on the main and red buttons is too faint (2.5:1 and 3.4:1; the minimum is 4.5:1). Status changes aren't announced to screen readers. There's no reduced-motion option. |
| 2 | Performance | 3 | The JavaScript is well optimised. But 8 animations run forever (shimmer, pulsing, spinning), so the browser repaints all the time during a run. |
| 3 | Theming | 2 | 9 colour variables exist, but about 30 colours are typed in directly instead, and a second block of CSS quietly overrides `.panel`, `button`, `.status-*` and `.muted`. |
| 4 | Responsive | 1 | There are no mobile layouts at all. At 390 px the provider header wraps to four lines and cuts off the rate-limit countdown and the status badge. 13 controls are smaller than the 44 px minimum for touch. |
| 5 | Implementation integrity | 2 | The 10 detector findings were each confirmed. Emoji are used as icons alongside proper SVG logos. The colour scheme is GitHub's exactly. |

---

## The punch list

Numbered for picking: "do 1–8" works. **Source** shows which audit found it: I for Impeccable, H for Hallmark, both when both did. Effort is rough: S is under 15 minutes, M is under an hour, L is more.

### Fix first: things that are broken for someone

| # | Severity | Source | Problem | Where | Fix | Effort |
|---|---|---|---|---|---|---|
| 1 | P1 | I | **Provider headers break on a phone.** The stats wrap to four lines, and the rate-limit countdown and badge are cut off, so on a phone you can't see that SoundCloud is cooling down. | `style.css:135` `.provider-header`, `:165` `.provider-stats` | Let the header wrap: the name and badge on the first line, the stats and limiter readout on a second. Shorten "1 playlist(s) · 6/6 tracks" to "6/6 tracks". | M |
| 2 | P1 | I | **Expand/collapse doesn't work from a keyboard.** The provider and playlist headers are click-only `<div>`s, and nothing tells a screen reader whether they're open. | `index.html` `ensureProviderSection`, `ensureCard` | Make them `<button>`s with `aria-expanded` and `aria-controls`, and add a visible focus outline. | M |
| 3 | P1 | I, H | **The reason a track failed only shows on mouse hover.** It's in a tooltip, so on a phone or tablet you can never see it. | `index.html` `updateTrackStatusDOM` (the `row.title` tooltip) | Show the reason as a second line under each failed or unavailable track. | S |
| 4 | P1 | I | **Button text is too faint.** White on the blue button is 2.5:1 and white on red is 3.4:1, below the 4.5:1 minimum. | `style.css:63`, `:475`, `:494` | Darken both button colours, or use dark text on them. | S |
| 5 | P1 | I | **Nothing respects "reduce motion".** Pulsing text, a shimmering bar, spinning emoji and a pulsing background all run forever. | `style.css:81, 174, 302, 417, 420, 437, 440, 504` | Animate only the row that's actually downloading. Add a reduced-motion setting that swaps movement for a static marker. | S |
| 6 | P2 | I | **Status changes aren't announced to screen readers.** "Running", "Stopping…" and the finished state change silently. | `index.html:12` `#status-indicator` | Add `role="status"` / `aria-live="polite"` to the indicator and the cooldown readout. | S |
| 7 | P2 | I | **The tabs aren't marked up as tabs**, so screen readers don't say which one is selected. | `index.html:15–19` | Add the standard tab markup (`role="tablist"`, `aria-selected`), and let the arrow keys move between tabs. | S |
| 8 | P2 | I | **Touch targets are too small.** The Log button is 23 px tall, the tabs 34 px, the checkboxes about 16 px. | `style.css:344` `.mini-btn`, `:33` `.tab` | Make every control at least 44 × 44 px on touch screens. | S |

### De-slop: the things that make it read as AI-generated

| # | Severity | Source | Problem | Where | Fix | Effort |
|---|---|---|---|---|---|---|
| 9 | critical | H, I | **Coloured stripes down the side of cards** (6 places). Hallmark rates this one of the most recognisable AI tells. | `style.css:220, 228, 234, 239, 566, 570` | Use an even thin border all round, and show status as a small dot or label next to the playlist name. | S |
| 10 | critical | H | **Boxes inside boxes inside boxes.** Each playlist card sits in a bordered provider box inside a bordered panel, and an open track list adds a fourth layer. | `style.css:44` `.panel`, `:122` `.provider-section`, `:198` `.playlist-card` | Keep one layer. Make each provider a heading with a thin rule under it, and each playlist a flat row. | M |
| 11 | major | H | **Emoji used as icons, mixed with real SVG logos.** 🎵 in the title, 📜 Log, the ✅🔄❌🚫⏳⏸ statuses, and ▶ ⏹ on buttons. Emoji look different on every device and clash with the proper SVG logos. | `index.html:11, 38, 51, 831–839, 1049, 1159–1164` | Pick one SVG icon set (Lucide fits) for status, play/stop and log. Or drop icons and use a coloured status dot plus a word. | M |
| 12 | major | H, I | **Coloured glow shadows on a dark page.** The running-card halo, button hover glows and the text glow on the status. | `style.css:221, 224, 235, 485, 497, 509` | On dark backgrounds, show depth with slightly lighter surfaces. Remove the coloured shadows. | S |
| 13 | major | H | **Gradients everywhere:** buttons, all five progress-bar colours, provider headers, the provider body. | `style.css:139, 145, 188, 289–305, 475, 486, 494, 498, 568` | Flat colours. The progress bar needs one fill colour per state, nothing more. | S |
| 14 | major | H | **Everything lifts on hover.** Cards and buttons move up 1 px *and* get a bigger shadow, and the provider header slides 4 px right. | `style.css:131, 144–146, 213–217, 482–486` | One small change per element (a colour shift is enough). Drop the movement. | S |
| 15 | major | H | **`transition: all`** on the tabs, the mini buttons and every button. | `style.css:36, 351, 479` | Name only the properties that should animate (background colour, border colour). | S |
| 16 | major | H | **A second "enhanced" block of CSS quietly overrides the first.** `.panel`, `button`, `.status-*` and `.muted` are each defined twice with different values, and about 30 colours are typed in directly instead of using variables. | `style.css:457–525` against `:44, 62, 80, 71` | Merge into one set of rules, put every colour in a variable, and delete the dead first versions. | M |
| 17 | major | H | **The colour scheme is GitHub's dark theme, exactly** (`#0d1117`, `#161b22`, `#58a6ff`, …), so it looks like a GitHub page rather than a music tool. | `style.css:2–10` | Choose its own scheme: warm or cool neutrals plus one accent colour. Spotify green, YouTube red and SoundCloud orange already appear in the logos, so the accent shouldn't be any of those. | M |
| 18 | major | H | **A progress percentage inside the bar *and* a count beside it**, plus a third count in the subtitle on finished cards. | `index.html` `syncAccordion` (`card.fill` text) | Keep the "175/182" count; remove the % text inside the bar. | S |
| 19 | major | H | **A "Log" button on every card**, even though a full log pane sits at the bottom of the page. | `index.html:1049` | Keep one, opened from the card's ⋯ or by clicking its status line, or filter the main log by playlist. | M |

### Polish

| # | Severity | Source | Problem | Where | Fix | Effort |
|---|---|---|---|---|---|---|
| 20 | minor | H | **Three dots instead of a real ellipsis:** "Loading...", "Waiting...", "Downloading...". | `index.html:67, 75, 254, 1164` | Use "…". | S |
| 21 | minor | H | **The parse preview is centred under left-aligned controls**, because `.muted` adds 20 px of padding and centring. | `style.css:522` | Split it into a centred "empty state" style and a left-aligned "helper text" style. | S |
| 22 | minor | H | **Tabs styled as old-fashioned folder tabs.** | `style.css:33–39` | Use an underlined tab strip or a segmented control. | S |
| 23 | minor | H | **Provider names and the "TRACKS" header in spaced-out capitals.** | `style.css:161–164, 386–392` | Normal capitalisation, at medium weight. | S |
| 24 | P3 | I | **`playlist.html` has 120+ `<audio>` players, one per row,** and the cramped table doesn't fit on a phone. | `playlist.html` | One shared player bar, with a play button per row. | M |

---

## What's working (keep these)

- **The way the Live tab updates** is better than most production dashboards. Each card is built once and then patched in place, nothing is searched for on every update, and track rows are only built when a card is opened. Any redesign has to keep this.
- **The real SVG logos** for YouTube, SoundCloud and Spotify, rather than lookalike emoji, are a good call. They should become part of the one icon set from #11.
- **Numbers line up in columns** in the counts and track numbers.
- **Error messages say why something happened**, not just that it did: the rate-limit countdown, "17 left — re-run to continue", "found 168 already downloaded". The writing is in good shape. The look needs to catch up.

## Suggested order

1. **#1–#8** (accessibility and phone layout) in one batch, about 2 hours. These are real problems for anyone on a phone or keyboard, and none of them depend on the visual direction.
2. **#9–#16** (remove the AI-generated look) in one batch, about 2 hours. It's mostly deleting CSS, so it's low risk.
3. **#17** (a colour scheme of its own) together with **#10** and **#11**, as a small redesign. I'd run Impeccable's `init` first (a five-minute interview that writes `PRODUCT.md`) so the new direction is written down, then check the result against both audits again.
4. **#18–#24** whenever convenient.

Re-run both audits after each batch to watch the score move.
