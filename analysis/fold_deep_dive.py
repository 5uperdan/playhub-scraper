"""
Fold seeding deep dive — MKtom vs MK_ifoughtthelore
====================================================
Investigates the 4 specific competitions where MKtom and MK_ifoughtthelore
were paired in Round 1. For each event, ranks all participants by ph_user_id
and shows whether the pairing is consistent with a fold-seeding model
(rank i paired with rank n-1-i).

Also prints all Round 1 pairings with their rank sums to visualise the
degree to which the whole event matches a fold pattern.
"""

import sqlite3

DB = "playhub.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

tom = conn.execute("SELECT uuid, ph_user_id FROM players WHERE name = 'MKtom'").fetchone()
iff = conn.execute("SELECT uuid, ph_user_id FROM players WHERE name = 'MK_ifoughtthelore'").fetchone()
print(f"MKtom             ph_user_id={tom['ph_user_id']}")
print(f"MK_ifoughtthelore ph_user_id={iff['ph_user_id']}")

comps = conn.execute(
    """
    SELECT m.competition_uuid
    FROM matches m
    JOIN rounds r ON r.uuid = m.round_uuid
    WHERE r.name = 'Round 1'
      AND ((m.player_a_uuid = ? AND m.player_b_uuid = ?)
        OR (m.player_a_uuid = ? AND m.player_b_uuid = ?))
""",
    (tom["uuid"], iff["uuid"], iff["uuid"], tom["uuid"]),
).fetchall()

for row in comps:
    cuuid = row["competition_uuid"]
    comp = conn.execute(
        """
        SELECT c.start_date, c.attended_player_count, v.name AS venue
        FROM competitions c JOIN venues v ON v.ph_uuid = c.venue_uuid
        WHERE c.uuid = ?
    """,
        (cuuid,),
    ).fetchone()
    print(f"\n{'='*60}")
    print(f"  {comp['start_date']}  {comp['venue']}  ({comp['attended_player_count']} players)")

    r1_players = conn.execute(
        """
        SELECT DISTINCT p.uuid, p.name, p.ph_user_id
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        JOIN players p ON p.uuid IN (m.player_a_uuid, m.player_b_uuid)
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
          AND p.ph_user_id IS NOT NULL
        ORDER BY CAST(p.ph_user_id AS INTEGER)
    """,
        (cuuid,),
    ).fetchall()

    players_list = list(r1_players)
    n = len(players_list)
    rank_of = {p["uuid"]: i for i, p in enumerate(players_list)}

    for i, p in enumerate(players_list):
        marker = ""
        if p["uuid"] == tom["uuid"]:
            marker = "  <-- MKtom"
        elif p["uuid"] == iff["uuid"]:
            marker = "  <-- MK_ifoughtthelore"
        print(f"    rank {i:2}  ph_user_id={p['ph_user_id']:>8}  {p['name']}{marker}")

    tom_rank = rank_of.get(tom["uuid"])
    iff_rank = rank_of.get(iff["uuid"])
    if tom_rank is not None and iff_rank is not None:
        diff = abs(tom_rank - iff_rank)
        fold_partner_of_tom = n - 1 - tom_rank
        print(f"\n  MKtom rank={tom_rank}, MK_iff rank={iff_rank}, diff={diff}")
        print(
            f"  Fold partner of MKtom would be rank {fold_partner_of_tom} -- matches MK_iff? {fold_partner_of_tom == iff_rank}"
        )

    all_pairs = conn.execute(
        """
        SELECT m.player_a_uuid, m.player_b_uuid
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
    """,
        (cuuid,),
    ).fetchall()

    print("\n  All R1 pairings by ph_user_id rank (fold sum should equal n-1):")
    pair_rows = []
    for pair in all_pairs:
        a, b = pair["player_a_uuid"], pair["player_b_uuid"]
        ra = rank_of.get(a)
        rb = rank_of.get(b)
        if ra is not None and rb is not None:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            pair_rows.append((lo, hi))
    pair_rows.sort()
    on_fold = 0
    for lo, hi in pair_rows:
        nm_lo = players_list[lo]["name"]
        nm_hi = players_list[hi]["name"]
        s = lo + hi
        fold_match = s == n - 1
        if fold_match:
            on_fold += 1
        tag = "  <-- FOLD" if fold_match else f"  (sum={s}, expect {n-1})"
        print(f"    {lo:2} vs {hi:2}  {tag}   {nm_lo} vs {nm_hi}")
    print(f"\n  Pairs perfectly on fold: {on_fold}/{len(pair_rows)} ({100*on_fold/len(pair_rows):.0f}%)")

conn.close()
