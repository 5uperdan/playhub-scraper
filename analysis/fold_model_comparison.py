"""
Full fold vs half fold model comparison
========================================
Across all events with >=8 players, tests two pairing models against
actual Round 1 pairings (players ranked by ph_user_id):

  Full fold: rank i paired with rank n-1-i  (pairs sum to n-1)
  Half fold: rank i paired with rank n//2+i (lower half meets upper half)

Both are compared against a shuffle baseline (random pairing within each event).

Key finding:
  Half fold: 19.4% match vs 8.4% random (+11pp)
  Full fold: 13.8% match vs 8.5% random (+5pp)

The half fold fits better and has more signal, suggesting PlayHub halves
the field and pairs across the two halves rather than top-to-bottom folding.
"""

import random as rng
import sqlite3
import statistics

DB = "playhub.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

comps = conn.execute(
    """
    SELECT uuid, attended_player_count FROM competitions
    WHERE attended_player_count >= 8
"""
).fetchall()

full_fold_hits = 0
half_fold_hits = 0
total_pairs = 0
total_comps = 0

full_fold_pct_per_comp = []
half_fold_pct_per_comp = []

for comp in comps:
    cuuid = comp["uuid"]
    r1_players = conn.execute(
        """
        SELECT DISTINCT p.uuid, p.ph_user_id
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        JOIN players p ON p.uuid IN (m.player_a_uuid, m.player_b_uuid)
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
          AND p.ph_user_id IS NOT NULL
        ORDER BY CAST(p.ph_user_id AS INTEGER)
    """,
        (cuuid,),
    ).fetchall()

    try:
        players = [(p["uuid"], int(p["ph_user_id"])) for p in r1_players]
    except (TypeError, ValueError):
        continue
    if len(players) < 4:
        continue

    n = len(players)
    rank_of = {uuid: i for i, (uuid, _) in enumerate(players)}

    pairs = conn.execute(
        """
        SELECT m.player_a_uuid, m.player_b_uuid
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
    """,
        (cuuid,),
    ).fetchall()

    comp_pairs = 0
    comp_full = 0
    comp_half = 0
    for pair in pairs:
        a, b = pair["player_a_uuid"], pair["player_b_uuid"]
        ra, rb = rank_of.get(a), rank_of.get(b)
        if ra is None or rb is None:
            continue
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        s = lo + hi
        comp_pairs += 1
        total_pairs += 1
        if s == n - 1:
            full_fold_hits += 1
            comp_full += 1
        if hi == lo + n // 2:
            half_fold_hits += 1
            comp_half += 1

    if comp_pairs > 0:
        total_comps += 1
        full_fold_pct_per_comp.append(comp_full / comp_pairs)
        half_fold_pct_per_comp.append(comp_half / comp_pairs)

print(f"Events analysed: {total_comps}, total R1 pairs: {total_pairs}")
print()
print(f"Full fold (rank i paired with rank n-1-i):")
print(f"  Pairs matching:     {full_fold_hits}/{total_pairs} = {100*full_fold_hits/total_pairs:.1f}%")
print(f"  Avg % per event:    {100*statistics.mean(full_fold_pct_per_comp):.1f}%")
print(f"  Median % per event: {100*statistics.median(full_fold_pct_per_comp):.1f}%")
print()
print(f"Half fold (rank i paired with rank n//2 + i):")
print(f"  Pairs matching:     {half_fold_hits}/{total_pairs} = {100*half_fold_hits/total_pairs:.1f}%")
print(f"  Avg % per event:    {100*statistics.mean(half_fold_pct_per_comp):.1f}%")
print(f"  Median % per event: {100*statistics.median(half_fold_pct_per_comp):.1f}%")
print()

full_majority = sum(1 for x in full_fold_pct_per_comp if x > 0.5)
half_majority = sum(1 for x in half_fold_pct_per_comp if x > 0.5)
print(f"Events where >50% pairs fit full fold: {full_majority}/{total_comps} ({100*full_majority/total_comps:.1f}%)")
print(f"Events where >50% pairs fit half fold: {half_majority}/{total_comps} ({100*half_majority/total_comps:.1f}%)")
print()

rng.seed(42)
rand_full = 0
rand_half = 0
rand_total = 0
for comp in comps:
    cuuid = comp["uuid"]
    r1_players = conn.execute(
        """
        SELECT DISTINCT p.uuid, p.ph_user_id
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        JOIN players p ON p.uuid IN (m.player_a_uuid, m.player_b_uuid)
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
          AND p.ph_user_id IS NOT NULL
    """,
        (cuuid,),
    ).fetchall()
    try:
        players = [(p["uuid"], int(p["ph_user_id"])) for p in r1_players]
    except (TypeError, ValueError):
        continue
    if len(players) < 4:
        continue
    n = len(players)
    players.sort(key=lambda x: x[1])
    rank_of = {uuid: i for i, (uuid, _) in enumerate(players)}
    uuids = [uuid for uuid, _ in players]
    rng.shuffle(uuids)
    for i in range(0, len(uuids) - 1, 2):
        ra, rb = rank_of[uuids[i]], rank_of[uuids[i + 1]]
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        rand_total += 1
        if lo + hi == n - 1:
            rand_full += 1
        if hi == lo + n // 2:
            rand_half += 1

print(f"Random baseline (shuffle within event):")
print(f"  Full fold random rate: {100*rand_full/rand_total:.1f}%")
print(f"  Half fold random rate: {100*rand_half/rand_total:.1f}%")

conn.close()
