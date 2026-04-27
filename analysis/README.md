# PlayHub Pairing Algorithm — Investigation Notes

This directory contains analysis scripts and findings from an empirical investigation into how PlayHub generates Round 1 Swiss pairings and assigns byes to odd-numbered player fields.

---

## Background

The investigation was triggered by observing that two specific players — MKtom (ph_user_id=43991) and MK_ifoughtthelore (ph_user_id=14445) — were repeatedly paired against each other in Round 1 across multiple separate tournaments. Given the large number of possible pairings, this seemed implausible under a random assignment. This prompted a systematic statistical investigation into whether PlayHub's pairing algorithm is deterministic and what inputs it uses.

---

## Key Concepts

### ph_user_id

Every PlayHub account has a globally assigned integer ID (`ph_user_id`, stored in the `players` table). This is assigned at account creation and appears to be strictly sequential — lower values correspond to older accounts. It is **not** event-local (i.e. the same player has the same ID at every event they attend). This field turned out to be the central variable in all findings below.

### Fold seeding

A common Swiss tournament pairing strategy for Round 1 is "fold" seeding — sort players by some key, then pair across the two halves of the list:

- **Full fold**: rank `i` paired with rank `n-1-i` (top meets bottom, second meets second-from-bottom, etc.)
- **Half fold**: rank `i` paired with rank `n//2 + i` (bottom half of the top half meets top of the bottom half, etc.)

Both produce large rank-difference pairings — top players are not paired with near-neighbours.

---

## Working Hypothesis: Three Likely Factors

Based on the evidence so far, Round 1 seeding and bye assignment appear to be influenced by at least three factors. None of these is fully proven in isolation — they are likely inputs to a single deterministic function:

1. **`ph_user_id`** — the strongest confirmed signal. Sorting players ascending by ph_user_id and applying a half-fold to the resulting list predicts Round 1 pairings at +11 percentage points above random. Byes consistently go to players in the bottom half of the ph_user_id distribution at each event. This is the most robustly tested factor.

2. **Venue** — the same players repeatedly receive byes at the same venue, in a way that cannot be fully explained by the consistent local player pool alone. Within-venue bye rank is 32% more consistent than across-venue. This suggests the venue UUID or some venue property may be a direct input to the sort or bye formula. It is not yet possible to separate whether the venue contributes independently or entirely through its effect on which players attend.

3. **Event format** — a trend exists in which Core Constructed events show a much stronger ph_user_id signal (bye near rank 0) than Non-Core Constructed events (bye more scattered, including at least one case at the highest-ranked player). This is based on a single venue (The Gamers' Emporium) and is not yet confirmed across the broader dataset. The non-Core Constructed link is unproven and has at least one significant outlier.

All three factors may combine into a single sort key — for example, `hash(venue_uuid, format, ph_user_id)` — rather than being applied as separate steps.

---

## Summary of Findings

### 1. Round 1 pairings are not random

Adjacent ph_user_id players (rank difference = 1) appear in Round 1 together at only **2.7%** of the rate you would expect from random pairings (16.1% random expectation). Large rank-difference pairings are correspondingly over-represented. This conclusively rules out random or sequential assignment.

### 2. The half-fold model on ph_user_id has the strongest signal

Across all stored events, applying a half-fold to players sorted ascending by ph_user_id predicts Round 1 pairings at **19.4%**, vs **8.4%** for a random baseline — an 11 percentage point uplift. The full-fold model achieves 13.8% (+5pp). The half-fold is a better fit.

### 3. ph_user_id is the sort key — names and hashes have no signal

Eight candidate sort keys were tested: ph_user_id, player name (lowercased, raw, MD5 hash, SHA-1 hash), composite keys (ph_user_id then name, name then ph_user_id), ph_user_id modulo 13, and random. **Only ph_user_id produced signal above random.** All name-based keys and hashes performed at the random baseline level (≈8.4%). This means PlayHub sorts by ph_user_id (or something that monotonically preserves its order) when constructing Round 1 seedings.

### 4. Byes go to players with low ph_user_id rank

Across 229 events with a detectable single bye, the bye recipient's ph_user_id rank (within the event, 0=lowest) has:
- **Mean = 0.161** (random would be 0.500)
- **30%** of byes go to the single player with the lowest ph_user_id at that event
- **Never** does a player in the top 50% of ph_user_id values at an event receive a bye

This is consistent with the sort being ascending on ph_user_id and byes being assigned to the lower end of the sorted list.

### 5. The bye is not always rank 0 — the formula is approximately n//4

The bye rank is not always the single lowest-ID player. Testing `n // 4` (integer division) as the predicted bye rank gives an exact hit in **20.1%** of events vs ~6–7% random — a significant but imprecise signal. The distribution of `(actual_bye_rank − n//4)` is centred near zero with spread of roughly ±3.

### 6. Event format correlates with bye position — trend observed, not proven

Analysis of The Gamers' Emporium (Swansea) — a venue with many stored events across different formats — revealed a format-level split:

- **Core Constructed** events (standard set championships and weekly constructed play): rank 0 gets the bye in **10/17 (59%)** of odd-player events, mean relative rank = 0.10. The 7 non-rank-0 cases cluster at ranks 1–3.
- **Non-Core Constructed** events (e.g. "Constructed", "Infinity Constructed"): only 3/9 hit rank 0, mean relative rank = 0.31, including one case where the **highest** ph_user_id player (rank 12/12) received the bye — a direct contradiction of the ph_user_id hypothesis as stated.

**Important caveat:** This analysis is based on a single venue and a small sample (26 events total, only 9 Non-Core Constructed). The non-Core Constructed link is unconfirmed and has at least one significant outlier. It is possible the scatter in non-Core events reflects a different algorithm, an additional input, or simply greater noise for a format with fewer rounds. This finding should not be treated as proven until replicated across other venues and event types.

### 7. Within-venue bye rank consistency is a genuine venue-level signal

Within-venue standard deviation of **relative** bye rank (0.127) is 32% lower than the overall standard deviation (0.187). Crucially, the bye rank is already expressed as a normalised fraction between 0 and 1 (0 = lowest ph_user_id in the event, 1 = highest), so this normalisation already controls for who attends. A consistent local player pool cannot explain lower within-venue variance in an already-normalised measure — the consistency must reflect something about the venue itself.

The depth of the effect at The Gamers' Emporium reinforces this. Across the whole dataset, 30% of byes go to the rank-0 player (already far above the ~7% random expectation for average event sizes). At TGE Core Constructed events specifically, 59% go to rank 0 — nearly twice the global rate and nearly nine times random. The same venue consistently pushes byes even further toward the lowest-ID player than the general trend would predict.

One possible mechanism is a **venue-level shuffle effect**: the algorithm may apply a deterministic shuffle or perturbation seeded by some venue property (UUID, numeric store ID, or similar) that modulates how far along the sorted list the bye slot lands. Some venues' seeds would produce byes near rank 0 (like TGE), while others produce more scattered positions — explaining both the reduced within-venue variance and the cross-venue variation that the ph_user_id signal alone cannot account for.

The same specific players also receive byes repeatedly at the same venue, which is a stronger observation than the stdev numbers alone convey. This is consistent with a venue input to a deterministic function: same players + same venue → same bye recipient.

### 8. The DannyB / TurtleBauer bye pattern is explained by adjacency

MK_DannyB (ph_user_id=14190) received the bye at two events; MK_TurtleBauer (ph_user_id=14238) received it at two others — all four events attended by both players. This is entirely consistent with a deterministic algorithm. The two players are nearly adjacent in ph_user_id (only MK_TripleJ, ph_user_id=14193, sits between them), so they typically occupy consecutive ranks in the event's sorted list. At Kingdom Gaming (regular MK player pool, no very-low-ID players), they sit at ranks 0–4 and a bye formula landing at ranks 2–4 hits one or the other depending on total player count `n`.

| Event | n | DannyB rank | TurtleBauer rank | Bye rank | Recipient |
|---|---|---|---|---|---|
| Players Paradice Jan-25 | 13 | 0 | 2 | 0 | DannyB |
| Wargames Workshop Jan-25 | 23 | 5 | 7 | 5 | DannyB |
| Kingdom Gaming Jan-25 | 21 | 2 | 4 | 4 | TurtleBauer |
| Kingdom Gaming Apr-25 | 9 | 0 | 2 | 2 | TurtleBauer |

### 9. Registration timestamps and registration IDs have no signal

Two additional API fields were tested as candidate sort keys:
- `registration_completed_datetime`: **−8.8pp** vs random (worse than random)
- `registration.id` (sequential registration identifier): **−10.5pp** vs random (also worse)
- API response order: **−5.3pp** vs random

Negative signals mean these orderings are actively anti-correlated with actual pairings — suggesting some internal transformation (e.g. reversal or shuffle) occurs relative to these orderings. They are not the sort key.

### 10. player_a / player_b assignment in stored match records is random

In 4,364 sampled R1 matches, `player_a` has the lower ph_user_id in **51.8%** of cases and `player_b` in **48.2%** — statistically indistinguishable from 50/50. The `player_a`/`player_b` field assignment does not encode sort order.

---

## Open Questions

- **Venue as a direct input**: The within-venue consistency in *normalised* bye rank (which already controls for player pool composition) is evidence that venue is a genuine factor, not just a proxy for who attends. The most likely mechanism is a venue-level shuffle or seed — e.g. the bye slot position is modulated by a value derived from the venue UUID or numeric store ID. Identifying the exact transform would require comparing the same players at multiple venues, or brute-forcing plausible hash functions against known bye recipient/venue combinations.
- **Exact bye slot formula**: What transformation of ph_user_id (if any) maps to the precise bye rank? The `n//4` formula is suggestive but only exactly correct 20% of the time, with spread of ±3.
- **Non-Core Constructed anomalies**: Why does the Nexus Night at TGE produce a bye at rank 12/12 (the highest ph_user_id in the event)? Is this a different algorithm, a data error, or evidence of an additional input that inverts the ordering?
- **Format effect at other venues**: Does the Core Constructed vs Non-Core Constructed split hold at venues other than TGE? This needs to be tested before treating format as a confirmed factor.
- **Random seed**: Is there a per-event random component (e.g. an event UUID seeding a shuffle) that explains the residual noise after accounting for ph_user_id, venue, and format?

---

## Scripts

### `r1_rank_diff.py`

Tests whether Round 1 pairings are random by comparing the distribution of ph_user_id rank-differences between actual R1 pairs and a simulated random pairing within the same events.

**How it works:** For each of 300 randomly sampled events, players who appeared in R1 are ranked by ph_user_id. The rank difference between each actual R1 pair is recorded. A parallel simulation randomly shuffles players within each event and records the expected rank differences. Both distributions are then compared.

**Key output:**
```
Diff     Actual%    Random%
   1       2.7%      16.1%   <-- adjacent almost never meet
   2       4.4%      14.3%
   ...
  >11     30.2%      10.8%   <-- large-diff pairings over-represented
Actual  median=7.0   Random  median=4.0
```

**Run:** `python analysis/r1_rank_diff.py` from the project root.

---

### `fold_deep_dive.py`

Investigates the 4 specific events where MKtom and MK_ifoughtthelore were paired in Round 1, to test whether a full-fold model on ph_user_id predicts their pairing and the rest of the event's R1 bracket.

**How it works:** For each of the 4 identified events, all R1 participants are ranked by ph_user_id. The script checks whether MKtom's fold partner (rank `n-1-i`) matches MK_ifoughtthelore's rank, then prints all R1 pairs with their rank sums (a perfect full fold has every pair summing to `n-1`).

**Key output:** In these 4 events, MKtom's full-fold partner does match MK_ifoughtthelore's rank, but only 40–70% of other pairs in the same event satisfy the full fold, suggesting the full fold is not the correct model for the whole event.

**Run:** `python analysis/fold_deep_dive.py` from the project root.

---

### `fold_model_comparison.py`

Systematically compares full-fold vs half-fold vs random across all stored events with ≥8 players.

**How it works:** For each event and each R1 pair, the script checks:
- **Full fold**: does the pair satisfy `rank_a + rank_b == n - 1`?
- **Half fold**: does the pair satisfy `|rank_a - rank_b| == n // 2`?

A random baseline re-shuffles players within each event and applies the same tests.

**Key results:**
```
Full fold:    13.8% of pairs match  vs 8.5% random  (+5.3pp)
Half fold:    19.4% of pairs match  vs 8.4% random  (+11.0pp)
```

The half fold has roughly twice the signal of the full fold, making it the preferred model.

**Run:** `python analysis/fold_model_comparison.py` from the project root.

---

### `sort_key_hypothesis.py`

Tests eight candidate sort keys for the half-fold model to identify which one PlayHub actually uses.

**How it works:** For each event and each candidate key, players are sorted by that key and the half-fold hit rate is measured against actual R1 pairings. The random baseline shuffles players within each event.

**Candidates tested:**

| Key | Description | Result vs random |
|---|---|---|
| `ph_user_id` | Numeric account ID | **+11pp** |
| `name_lower` | Display name, lowercased | ≈0pp |
| `name_raw` | Display name, case-sensitive | ≈0pp |
| `name_hash_md5` | MD5 of display name | ≈0pp |
| `name_hash_sha1` | SHA-1 of display name | ≈0pp |
| `phid_then_name` | ph_user_id primary, name tiebreak | +11pp |
| `name_then_phid` | Name primary, ph_user_id tiebreak | ≈0pp |
| `phid_mod13` | ph_user_id modulo 13 | ≈0pp |
| `random_baseline` | Shuffled within event | baseline |

**Conclusion:** ph_user_id is the only key with meaningful signal. Name-based keys — even composites where ph_user_id is a tiebreak — perform at random when name is the primary sort. PlayHub sorts primarily by ph_user_id.

**Run:** `python analysis/sort_key_hypothesis.py` from the project root.

---

### `bye_analysis.py`

Infers the bye recipient in each odd-player-count event and tests whether byes are assigned based on ph_user_id position.

**How it works:** Byes are not stored explicitly. They are inferred by finding players who appear in matches in rounds 2+ but are absent from Round 1 match records. For each such player, their ph_user_id rank within the event (sorted ascending) is computed as a fraction between 0 (lowest ID) and 1 (highest ID).

**Key results:**
```
Events with clean single bye: 229
Mean relative rank: 0.161   (random: 0.500)
Median relative rank: 0.083
Bye is rank 0 (lowest ph_user_id): 30.1% of events
Bye is rank n-1 (highest ph_user_id): 0.0% of events (never observed)
```

The bye player's rank is always in the bottom half (0–50th percentile) of the event's ph_user_id distribution. The rank-0 case (30%) is far above the random expectation of ~7% for average event sizes.

**Run:** `python analysis/bye_analysis.py` from the project root.

---

## Temporary / Ad-hoc Scripts

The following scripts were created during the investigation and stored in `/tmp/`. They are not in this directory but are documented here for reference.

| Script | Purpose |
|---|---|
| `/tmp/ordering_investigation.py` | Tests whether `player_a` vs `player_b` field assignment encodes ph_user_id order (result: ~50/50, no encoding); fetches live API registration timestamps for MK_DannyB's 8 odd-player events to check if bye recipient registered last (result: no consistent pattern) |
| `/tmp/bye_formula.py` | Tests whether `n // 4` predicts the bye rank exactly across 229 events (result: 20.1% exact hit, spread of ±3 around prediction) |
| `/tmp/venue_bye.py` | Compares within-venue vs overall consistency of bye ranks across 145 venues. Mean within-venue stdev = 0.127, overall stdev = 0.187; ratio = 0.68 |
| `/tmp/tge_analysis.py` | Fetches all 99 The Gamers' Emporium events from the API and tests bye ranks across 26 complete odd-player events. Core Constructed: 10/17 rank-0 byes; Non-Core Constructed: 3/9 rank-0 byes |
| `/tmp/tge_formats.py` | Cross-tabulates the TGE bye data by gameplay format (Core Constructed vs Constructed/Infinity Constructed) |
| `/tmp/q4events.py` | Prints detailed R1 pairings for the 4 events where DannyB and TurtleBauer both attended and one received the bye |

---

## API Fields Explored

The PlayHub registrations endpoint (`/events/{id}/registrations/`) returns fields beyond what is stored in the database. The following were tested as potential sort keys or bye inputs:

| Field | Tested as | Result |
|---|---|---|
| `user.id` | Identical to ph_user_id — same signal | +11pp |
| `registration_completed_datetime` | Sort key for half-fold | **−8.8pp** (worse than random) |
| `registration.id` | Sort key for half-fold | **−10.5pp** (worse than random) |
| API response order | Natural ordering of API results | −5.3pp |
| `user.country_code` | — | Not tested |
| `matches_won / matches_lost` | — | Not applicable (no prior history at event time) |

The negative signals for registration timestamp and registration ID are notable — they are not merely uninformative, they are actively anti-correlated with pairings. This may reflect that the API returns registrations in reverse-chronological order (most recent first), meaning later registrants appear earlier in the API response, producing an inverted ordering.

---

## Data Sources

- **Database**: `playhub.db` — SQLite, populated by `main.py sync` and `main.py scrape`
- **API base**: `https://api.cloudflare.ravensburgerplay.com/hydraproxy/api/v2/`
- **Match data endpoint**: `/tournament-rounds/{round_id}/matches/`
- **Registration data endpoint**: `/events/{event_id}/registrations/`
- **Store events endpoint**: `/events/?store_id={numeric_store_id}`
- **Round 1 match data is not available via API for older events** (returns 404 for round IDs below approximately 100,000)
