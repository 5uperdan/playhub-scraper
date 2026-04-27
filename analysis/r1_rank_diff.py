"""
Round 1 pairing rank-difference distribution
=============================================
For each competition, rank Round 1 participants by ph_user_id ascending.
Compare the distribution of rank-differences between actual R1 pairs vs
a simulated random pairing within the same event.

Key finding: adjacent ph_user_id players almost never meet in Round 1
(2.7% actual vs 16.1% random). Large-diff pairings are over-represented.
This is inconsistent with sequential seeding and consistent with a half-fold
on some ordering correlated with ph_user_id.
"""

import random as rng
import sqlite3
import statistics

DB = "playhub.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

all_actual_diffs = []
all_random_diffs = []

comps = conn.execute(
    """
    SELECT uuid, attended_player_count FROM competitions
    WHERE attended_player_count >= 8
    ORDER BY RANDOM() LIMIT 300
"""
).fetchall()

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
    players.sort(key=lambda x: x[1])
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
    for pair in pairs:
        a, b = pair["player_a_uuid"], pair["player_b_uuid"]
        if a in rank_of and b in rank_of:
            all_actual_diffs.append(abs(rank_of[a] - rank_of[b]))
    uuids_only = [uuid for uuid, _ in players]
    rng.shuffle(uuids_only)
    for i in range(0, len(uuids_only) - 1, 2):
        all_random_diffs.append(abs(rank_of[uuids_only[i]] - rank_of[uuids_only[i + 1]]))

print(f"Pairs: {len(all_actual_diffs)} actual, {len(all_random_diffs)} simulated")
at = len(all_actual_diffs)
rt = len(all_random_diffs)
print()
print(f"{'Diff':>6}  {'Actual%':>9}  {'Random%':>9}")
for d in range(1, 12):
    ap = 100 * sum(1 for x in all_actual_diffs if x == d) / at
    rp = 100 * sum(1 for x in all_random_diffs if x == d) / rt
    marker = " <--" if abs(ap - rp) > 5 else ""
    print(f"  {d:4}    {ap:8.1f}%  {rp:8.1f}%{marker}")
over_a = 100 * sum(1 for x in all_actual_diffs if x > 11) / at
over_r = 100 * sum(1 for x in all_random_diffs if x > 11) / rt
print(f"   >11    {over_a:8.1f}%  {over_r:8.1f}%")
print()
print(f"Actual  median={statistics.median(all_actual_diffs):.1f}  mean={statistics.mean(all_actual_diffs):.2f}")
print(f"Random  median={statistics.median(all_random_diffs):.1f}  mean={statistics.mean(all_random_diffs):.2f}")

conn.close()
