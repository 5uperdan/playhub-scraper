"""
Sort key hypothesis testing
============================
Tests multiple candidate sort keys for the half-fold pairing model against
actual Round 1 pairings across all events.

Candidates tested:
  ph_user_id         — numeric account ID (globally sequential)
  name_lower         — player display name, lowercased
  name_raw           — player display name, case-sensitive
  name_hash_md5      — MD5 hash of display name
  name_hash_sha1     — SHA-1 hash of display name
  phid_then_name     — ph_user_id as primary, name as tiebreak
  name_then_phid     — name as primary, ph_user_id as tiebreak
  phid_mod13         — ph_user_id modulo 13 (arbitrary modular key)
  random_baseline    — shuffled within each event

Key finding:
  ph_user_id is the only key with significant signal (+11pp above random).
  All name-based and hash-based keys are indistinguishable from random.
  This strongly suggests PlayHub sorts by ph_user_id (or something that
  preserves its ordering monotonically) when constructing Round 1 pairings.
"""

import hashlib
import random as rng
import sqlite3
import statistics

DB = "playhub.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def half_fold_matches(players_sorted, pairs_by_uuid):
    """Count Round 1 pairs that match a half-fold on the given sort order."""
    n = len(players_sorted)
    rank_of = {uuid: i for i, (uuid, *_) in enumerate(players_sorted)}
    hits = 0
    total = 0
    for a, b in pairs_by_uuid:
        ra, rb = rank_of.get(a), rank_of.get(b)
        if ra is None or rb is None:
            continue
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        total += 1
        if hi == lo + n // 2:
            hits += 1
    return hits, total


comps = conn.execute(
    """
    SELECT uuid, attended_player_count FROM competitions
    WHERE attended_player_count >= 8
"""
).fetchall()

models = {
    "ph_user_id": lambda p: int(p["ph_user_id"]) if p["ph_user_id"] else 0,
    "name_lower": lambda p: p["name"].lower(),
    "name_raw": lambda p: p["name"],
    "name_hash_md5": lambda p: hashlib.md5(p["name"].encode()).hexdigest(),
    "name_hash_sha1": lambda p: hashlib.sha1(p["name"].encode()).hexdigest(),
    "phid_then_name": lambda p: (int(p["ph_user_id"]) if p["ph_user_id"] else 0, p["name"].lower()),
    "name_then_phid": lambda p: (p["name"].lower(), int(p["ph_user_id"]) if p["ph_user_id"] else 0),
    "phid_mod13": lambda p: int(p["ph_user_id"]) % 13 if p["ph_user_id"] else 0,
    "random_baseline": None,
}

results = {m: {"hits": 0, "total": 0} for m in models}
rng.seed(42)

for comp in comps:
    cuuid = comp["uuid"]
    r1_players = conn.execute(
        """
        SELECT DISTINCT p.uuid, p.name, p.ph_user_id
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        JOIN players p ON p.uuid IN (m.player_a_uuid, m.player_b_uuid)
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
    """,
        (cuuid,),
    ).fetchall()

    players = list(r1_players)
    if len(players) < 4:
        continue

    phid_present = sum(1 for p in players if p["ph_user_id"])

    pairs_raw = conn.execute(
        """
        SELECT m.player_a_uuid, m.player_b_uuid
        FROM matches m
        JOIN rounds r ON r.uuid = m.round_uuid
        WHERE m.competition_uuid = ? AND r.name = 'Round 1'
    """,
        (cuuid,),
    ).fetchall()
    pairs_by_uuid = [(row["player_a_uuid"], row["player_b_uuid"]) for row in pairs_raw]

    for model_name, key_fn in models.items():
        if model_name == "random_baseline":
            n = len(players)
            uuids = [p["uuid"] for p in players]
            rank_of = {uuid: i for i, uuid in enumerate(uuids)}
            shuffled = uuids[:]
            rng.shuffle(shuffled)
            for i in range(0, len(shuffled) - 1, 2):
                a, b = shuffled[i], shuffled[i + 1]
                ra, rb = rank_of[a], rank_of[b]
                lo, hi = (ra, rb) if ra < rb else (rb, ra)
                results[model_name]["total"] += 1
                if hi == lo + n // 2:
                    results[model_name]["hits"] += 1
            continue

        needs_phid = "phid" in model_name or model_name == "ph_user_id"
        if needs_phid and phid_present < len(players) * 0.8:
            continue

        try:
            sorted_players = sorted(players, key=key_fn)
        except Exception:
            continue

        sorted_with_uuid = [(p["uuid"],) for p in sorted_players]
        h, t = half_fold_matches(sorted_with_uuid, pairs_by_uuid)
        results[model_name]["hits"] += h
        results[model_name]["total"] += t

print(f"{'Model':<22}  {'Hits':>6}  {'Total':>6}  {'%':>7}  {'vs random':>10}")
print("-" * 60)
random_pct = results["random_baseline"]["hits"] / max(results["random_baseline"]["total"], 1) * 100
for model_name, data in results.items():
    t = data["total"]
    h = data["hits"]
    if t == 0:
        continue
    pct = 100 * h / t
    delta = pct - random_pct
    print(f"  {model_name:<20}  {h:6}  {t:6}  {pct:6.1f}%  {delta:+.1f}pp")

conn.close()
