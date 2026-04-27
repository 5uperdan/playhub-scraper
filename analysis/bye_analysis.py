"""
Bye player analysis
====================
Infers who received a bye in each odd-player-count event by finding the
player who appears in later rounds but has no Round 1 match.

Ranks bye recipients by ph_user_id within each event to test whether the
bye is assigned based on ph_user_id ordering.

Key findings:
  - The bye player's ph_user_id rank mean = 0.161 (random would be 0.500)
  - 30% of byes go to the single player with the lowest ph_user_id in the event
    (random expectation ~7%)
  - No player in the top 50% of ph_user_ids at an event has ever received a bye
    across 234 analysed events

This strongly suggests PlayHub sorts ascending by ph_user_id and gives the
bye to the player at the bottom of the sorted list (i.e. lowest ID).
"""

import sqlite3
import statistics

DB = "playhub.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

danny = conn.execute("SELECT uuid, ph_user_id, name FROM players WHERE name LIKE 'MK_Danny%'").fetchall()
for p in danny:
    print(f"Found: {p['name']}  uuid={p['uuid']}  ph_user_id={p['ph_user_id']}")
print()

odd_comps = conn.execute(
    """
    SELECT c.uuid, c.start_date, c.attended_player_count, v.name AS venue,
           s.display_name AS season
    FROM competitions c
    JOIN venues v ON v.ph_uuid = c.venue_uuid
    LEFT JOIN set_championship_types s ON s.uuid = c.set_championship_type_uuid
    WHERE c.attended_player_count % 2 = 1
      AND c.attended_player_count >= 7
"""
).fetchall()

print(f"Odd-player-count events: {len(odd_comps)}")
print()

bye_records = []

for comp in odd_comps:
    cuuid = comp["uuid"]

    all_match_players = conn.execute(
        """
        SELECT DISTINCT p.uuid, p.name, p.ph_user_id
        FROM matches m
        JOIN players p ON p.uuid IN (m.player_a_uuid, m.player_b_uuid)
        WHERE m.competition_uuid = ?
    """,
        (cuuid,),
    ).fetchall()
    all_match_uuids = {p["uuid"] for p in all_match_players}

    r1_uuids = set()
    for row in conn.execute(
        """
        SELECT m.player_a_uuid, m.player_b_uuid
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
    """,
        (cuuid,),
    ).fetchall():
        r1_uuids.add(row["player_a_uuid"])
        r1_uuids.add(row["player_b_uuid"])

    bye_uuids = all_match_uuids - r1_uuids
    if len(bye_uuids) != 1:
        continue

    bye_uuid = next(iter(bye_uuids))
    bye_player = conn.execute("SELECT uuid, name, ph_user_id FROM players WHERE uuid=?", (bye_uuid,)).fetchone()

    r1_with_id = [p for p in all_match_players if p["ph_user_id"] and p["uuid"] in r1_uuids]

    bye_phid = int(bye_player["ph_user_id"]) if bye_player["ph_user_id"] else None
    if bye_phid is None:
        continue

    all_with_phid = list(r1_with_id) + [bye_player]
    try:
        all_sorted = sorted(all_with_phid, key=lambda p: int(p["ph_user_id"]))
    except (ValueError, TypeError):
        continue

    rank_of_bye = next(i for i, p in enumerate(all_sorted) if p["uuid"] == bye_uuid)
    total_with_phid = len(all_sorted)

    bye_records.append(
        {
            "rank": rank_of_bye,
            "n": total_with_phid,
            "relative": rank_of_bye / (total_with_phid - 1) if total_with_phid > 1 else 0,
            "name": bye_player["name"],
            "ph_user_id": bye_phid,
            "date": comp["start_date"],
            "venue": comp["venue"],
            "season": comp["season"],
            "uuid": bye_uuid,
        }
    )

print(f"Events with clean single bye and ph_user_id data: {len(bye_records)}")
print()

relatives = [r["relative"] for r in bye_records]
print(f"Bye player ph_user_id rank (0=lowest ID in event, 1=highest ID):")
print(f"  Mean:   {statistics.mean(relatives):.3f}  (random expect 0.500)")
print(f"  Median: {statistics.median(relatives):.3f}")
print(f"  Stdev:  {statistics.stdev(relatives):.3f}")
print()

lowest = sum(1 for r in bye_records if r["rank"] == 0)
highest = sum(1 for r in bye_records if r["rank"] == r["n"] - 1)
avg_n = statistics.mean(r["n"] for r in bye_records)
print(f"  Bye is rank 0 (lowest ph_user_id):    {lowest}/{len(bye_records)} = {100*lowest/len(bye_records):.1f}%")
print(f"  Bye is rank n-1 (highest ph_user_id): {highest}/{len(bye_records)} = {100*highest/len(bye_records):.1f}%")
print(f"  Random expectation per extreme: ~{100/avg_n:.1f}% (avg event size {avg_n:.0f})")
print()

buckets = [0] * 10
for r in bye_records:
    buckets[min(int(r["relative"] * 10), 9)] += 1
total = len(bye_records)
print("Decile distribution of bye player's ph_user_id rank:")
for i, count in enumerate(buckets):
    bar = "#" * int(count * 40 / max(buckets))
    print(f"  {i*10:3}-{i*10+9:3}%  {count:4}  {100*count/total:5.1f}%  {bar}")
print()

# Show MK_DannyB's byes specifically
danny_uuid = next((p["uuid"] for p in danny if p["uuid"]), None)
if danny_uuid:
    danny_byes = [r for r in bye_records if r["uuid"] == danny_uuid]
    print(f"MK_DannyB bye events ({len(danny_byes)}):")
    for r in danny_byes:
        print(
            f"  {r['date']}  {r['venue']}  rank={r['rank']}/{r['n']-1}  (rel={r['relative']:.2f})  ph_user_id={r['ph_user_id']}"
        )

conn.close()
