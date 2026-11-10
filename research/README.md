# Rhetoric study: design

Written 2026-11-10, before any transcript or chat was read for this purpose. Predictions are in
`rhetoric-predictions.md`. They are committed first and never edited; additions go at the end with a date.
Results go in a separate `rhetoric-results.md`, misses included.

Method rules come from the owner's socialscope project (`carrier3rd/socialscope`, `SKILL.md`, `docs/research-methods.md`):
predictions before data, every quote checked against the source, flags are leads for a person to check, never proof.

## Questions
1. **Topics:** what share of each stream's time goes to each topic (Israel/Palestine, US politics, US foreign policy,
   Russia/Ukraine, react content, games, ...)?
2. **Hasan's rhetoric:** how often does each category below appear in what Hasan himself says, endorsed rather than quoted
   or criticised, with a timestamped quote for every instance?
3. **Chat:** for each stream, what share of messages and what share of chatters endorse each category? How concentrated
   is it (how many accounts write most of it)? Which accounts (private report only, see Privacy)?
4. **Spread (later):** after Hasan uses a framing, does it rise in chat, and how fast?

## Two definitions, side by side
"Antisemitic" is reported under both main definitions, because they disagree mostly about Israel:
- **IHRA** Working Definition (2016) and its 11 examples, 7 of which mention Israel.
- **Jerusalem Declaration (JDA, 2021):** "discrimination, prejudice, hostility or violence against Jews as Jews", with 15
  guidelines; it treats evidence-based criticism of Israel, comparisons (apartheid, settler-colonialism), BDS and harsh
  political speech as not antisemitic on their face.

Classic antisemitism (slurs, Jewish control, dual loyalty, collective blame, blood libel, Holocaust denial) counts under
both. Every result table has an IHRA column and a JDA column.

## Categories and tiers (`watchlist.csv`)
| tier | meaning | counted as antisemitic |
|---|---|---|
| explicit | slurs, Nazi codes, calls for violence, Holocaust denial terms | both definitions |
| trope | classic tropes (control, dual loyalty, blood libel, Khazars, Soros/Rothschild/globalist used about Jews) | both, when aimed at Jews |
| contested | Israel-Nazi comparisons, Holocaust inversion, "Zio", denying Israel's right to exist, "river to the sea", "Holocaust industry", Oct 7 praise | IHRA yes or context; JDA no on its face |
| talking_point | a named figure's argument or a documented narrative | not a hate label; counted for prevalence and stance |
| charged | loaded vocabulary ("Amerikkka", "imperial core") | no |
| mention / topic | names and topic words; used as denominators | no |

Categories: `antisemitism`, `holocaust_distortion`, `israel`, `violence`, `finkelstein`, `russia`, `anti_us`,
`anti_liberal` (added 2026-11-10 at the owner's request), `topic`,
plus the 18 Kremlin narratives in `kremlin_narratives.csv` (copied from socialscope, each with rating and source).

**Dog-whistle glossary:** `dogwhistles_allen.csv` holds the 73 antisemitic entries of the glossary from Mendelsohn, Le
Bras, Choi & Sap, "From Dogwhistles to Bullhorns" (ACL 2023; MIT licence, `dogwhistles_allen.LICENSE`;
github.com/juliamendelsohn/dogwhistles), each with its sources. 46 entries are ordinary words on a politics stream
("deep state", "Hollywood elite", "Zionist", "bankers", "Israel Lobby"), marked `needs_context`. They count as
candidates only when the same message also mentions Jews or Israel, and Claude codes them like everything else. A few
surface forms are dropped outright as unusable ("blue", "echo", "shut it down", "the Fed").

**Works:** `bibliography.md` lists Finkelstein's and Mearsheimer's books and key articles, and which claims from each
the watchlist tracks.

### Holocaust distortion
From the IHRA Working Definition of Holocaust Denial and Distortion (2013): denying the gas chambers, mass shootings or
the genocide's intent; gross minimisation of the death toll; blaming Jews for their own genocide; casting it as
positive; and, among later forms in IHRA's *Understanding Holocaust Distortion* (2021), accusing Jews of "using" the
Holocaust for gain and using Holocaust imagery for unrelated political purposes.

### Finkelstein: claims, not the man
Norman Finkelstein is tracked as a source of specific claims, so the study doesn't depend on a verdict about him:
- **"The Holocaust Industry"** (2000): that the American Jewish establishment exploits Holocaust memory for money and to
  shield Israel. IHRA's 2021 guide lists "accusing Jews of using the Holocaust" as distortion; Finkelstein aims the
  charge at organisations, so it is coded `contested`. Reception was split (Traverso: "a core of truth... lends itself
  to the worst uses").
- **Oct 7 framing:** his Oct 7, 2023 essay praising a "heroic resistance" ("it warms every fiber of my soul"), and
  describing the attackers as concentration-camp inmates who "burst the gates" (Piers Morgan) and comparing them to the
  Warsaw Ghetto uprising (Jewish Insider, Jan 2024).
- **Denial of the Oct 7 sexual violence** (reported from a podcast; to verify). The UN Special Representative on sexual
  violence in conflict found "reasonable grounds to believe" it occurred (mission report, March 2024).

### Mearsheimer and Russia apologia
John Mearsheimer is tracked the same way:
- **"The West's fault":** NATO and EU expansion and Western support for Ukraine's 2014 revolution caused the crisis
  ("Why the Ukraine Crisis Is the West's Fault", *Foreign Affairs*, 2014). Critics: McFaul (Russia waited a decade after
  enlargement; the change was inside Russian politics), Stallard (*New Statesman*), the *Irish Times* (2023).
- **"Putin has no imperial ambitions"** (*New Yorker*, 2022; *Oxford Political Review*, 2023), against Putin's own
  statements that Ukraine is not a real country and that Russians and Ukrainians are "one people".
- **Ukraine as a Western pawn** with no agency of its own.
- The documented false Kremlin narratives (Bucha staged, US biolabs, Ukraine a Nazi regime, ...) come from
  `kremlin_narratives.csv`.

### Anti-liberal framing (from the left)
Contempt for liberals and liberal democracy: "libs"/"shitlibs"/"liberal Zionist" as insults, "liberals are worse than
fascists" ("scratch a liberal..."), "uniparty"/"blue MAGA", "electoralism" and "bourgeois democracy". Mostly `charged`
vocabulary, counted for prevalence. The violent memes ("liberals get the bullet too", "up against the wall") are
`explicit`. Right-wing anti-liberal insults (soyboy, NPC) are in the Allen glossary's anti-liberal persona and not
imported, since they mark the other side.

### Anti-American framing
Criticising a US policy is not extremism. Following the socialscope commentator-valence study, every US-related segment
is coded for **target** (administration / agency / company / system) and **stance**. Flags are for system-level framings:
the US as the world's worst terrorist or evil (Hasan, Axios 2026: "pound for pound" worse than al-Qaeda), "America
deserved 9/11" (Hasan, 2019), "Death to America", and the Kremlin/Beijing state-media themes catalogued by EUvsDisinfo
and DFRLab (US decline and hegemony, colour revolutions, Europe as a US vassal). In the socialscope pilot, about 19 of
23 of Hasan's coded US-action segments were aimed at agencies, companies or the system rather than an administration.

## Method
1. **Find candidates (laptop, free):** run every `watchlist.csv` and `kremlin_narratives.csv` pattern over chat
   messages (live log, including messages mods later deleted, plus the replay) and over transcript turns.
2. **Code candidates (Claude via the API):** each candidate goes with its context: the 10 chat messages before it and
   what Hasan was saying in the 30 s around it. Claude returns category, **stance** (endorse / report or quote /
   condemn / joke or bait / unclear), target (for US items), and a verbatim quote that is checked against the source.
   **Only `endorse` counts toward the totals**; the others are reported separately.
3. **Measure what the keywords miss:** Claude also codes a random sample of 500 non-candidate chat messages per stream
   and the transcript windows with no candidate. That gives an estimate of coded or novel language the list misses.
4. **Topics:** each 60 s transcript window gets one topic from a fixed list; stream share = windows per topic / total.
5. **Check the coder:** the owner hand-labels a random 100 coded items before results are read; agreement is reported.

## Metrics, per stream and pooled
- Topic share of stream time.
- Hasan: endorsed instances per category per stream-hour, with quote and timestamp; IHRA count and JDA count.
- Chat: % of messages and % of distinct chatters endorsing each category; among messages on Israel/Jews
  (`topic_*`), the share that crosses each definition; concentration (share of flagged messages from the top 1% / 10%
  of flagged accounts); flag rate in deleted vs kept messages.

## Privacy
Per the owner's choice (2026-11-10), the per-account report names Twitch accounts. It stays private: written under
`archive/` (masked from the public site), never committed, never published. Anything shared shows aggregates only,
following socialscope's rule against publishing per-person labels. No linking of chat accounts to other platforms or
real identities.

## Limits
- The 5 pilot VODs were picked for variety (solo, guest, react, DMCA-muted, short), not at random.
- Twitch AutoMod and mods remove messages; the replay lacks them, while the live logger keeps their text (flagged
  deleted). The live logger only covers streams since 2026-11-09.
- Emotes, sarcasm, copypasta and bait are hard to code. Stance coding is the main defence, and step 5 measures it.
- Transcript speakers are `Unknown_*` until Hasan's voiceprint is enrolled, so guest and clip speech can't be told apart
  from Hasan's until then.

## Sources
- IHRA Working Definition of Antisemitism (2016): https://holocaustremembrance.com/wp-content/uploads/2024/01/IHRA-non-legally-binding-working-definition-of-antisemitism-1.pdf
- IHRA Working Definition of Holocaust Denial and Distortion (2013): https://www.worldjewishcongress.org/en/working-definition-of-holocaust-denial-and-distortion
- IHRA, *Understanding Holocaust Distortion* (2021): https://holocaustremembrance.com/wp-content/uploads/2023/09/Understanding-Holocaust-Distortion-Contexts-Influences-and-Examples-IHRA.pdf
- Jerusalem Declaration on Antisemitism (2021): https://jerusalemdeclaration.org/
- Antisemitism Policy Trust, *Glossary of Antisemitic Terms* (2023): https://antisemitism.org.uk/wp-content/uploads/2026/06/6847-Antisem-Glossary-2023-10-WEB.pdf
- ADL Hate on Display / extremism terms: https://www.adl.org/resources/hate-symbols/search, https://extremismterms.adl.org/glossary/khazars
- Finkelstein, *The Holocaust Industry* (2000); reception: https://en.wikipedia.org/wiki/The_Holocaust_Industry, https://www.salon.com/2000/08/30/finkelstein_2
- Finkelstein on Oct 7: https://jewishinsider.com/2024/01/jamaal-bowman-norman-finkelstein-oct-7-holocaust-antisemitism/, https://www.mediaite.com/uk/concentration-camp-inmates-norman-finkelstein-on-hamas-terror-attack-in-piers-morgan-interview
- Mearsheimer, "Why the Ukraine Crisis Is the West's Fault", *Foreign Affairs* (2014): https://www.foreignaffairs.com/node/1069022; exchange "Faulty Powers": https://www.foreignaffairs.com/node/1133702
- Mearsheimer interview, *Oxford Political Review* (2023): https://oxfordpoliticalreview.com/2023/01/12/ive-been-attacked-not-with-facts-and-logic-but-personally-john-mearsheimer-on-the-war-in-ukraine/
- Critics: https://www.newstatesman.com/world/europe/2023/10/john-mearsheimers-incorrect-views-on-everything, https://www.irishtimes.com/opinion/2023/02/14/you-cannot-blame-the-west-for-the-war-in-ukraine
- EUvsDisinfo cases on US narratives: https://euvsdisinfo.eu/report/us-provoking-russia-due-to-decline-hegemony/, https://www.euvsdisinfo.eu/report/us-organizes-destabilization-all-over-the-world-inspires-colour-revolutions/
- DFRLab (2021): https://dfrlab.org/2021/09/07/chinese-russian-narratives-following-us-afghanistan-withdrawal-present-a-us-in-decline/
- Hasan's prior statements (partisan outlets reporting direct quotes): https://www.fox9.com/news/jesse-ventura-defends-streamer-hasan-piker-minneapolis-event, https://foxnews.com/media/hasan-piker-admits-dialed-down-opinions-after-public-democratic-backlash
- Still to fetch and cite properly: the UN SRSG-SVC mission report (March 2024); the AJC #TranslateHate glossary (the site
  refused automated access).
