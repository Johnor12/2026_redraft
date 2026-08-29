# Round 1 plan (slot-agnostic fallback)

Generated 2026-08-29 from the refreshed rankings.json (quantile-repriced pool),
before draft slots were assigned — ESPN currently serves a filler order, so
slot-specific outputs are ignored here. Use if the system is down at my pick:
take the highest name still available. Gain = projected points above the
position's waiver-wire baseline (slot-independent); gains are on the new
truncated-quantile scale and not comparable to earlier plans. Room rank =
consensus rank across the opponent boards — a proxy for how soon the room
takes him.

| #  | Player              | Pos | Gain | Room rank |
|----|---------------------|-----|------|-----------|
| 1  | Jahmyr Gibbs        | RB  | 229  | 1         |
| 2  | Bijan Robinson      | RB  | 223  | 2         |
| 3  | Ashton Jeanty       | RB  | 222  | 14        |
| 4  | De'Von Achane       | RB  | 216  | 12        |
| 5  | Christian McCaffrey | RB  | 215  | 11        |
| 6  | Ja'Marr Chase       | WR  | 210  | 3         |
| 7  | Jonathan Taylor     | RB  | 205  | 7         |
| 8  | Amon-Ra St. Brown   | WR  | 204  | 6         |
| 9  | Puka Nacua          | WR  | 202  | 4         |
| 10 | Derrick Henry       | RB  | 200  | 28        |
| 11 | James Cook          | RB  | 198  | 9         |
| 12 | Josh Jacobs         | RB  | 194  | 42        |

## Tiers

- Gibbs / Bijan / Jeanty: top tier within 7 pts of each other.
- Achane / CMC: a half-step back, effectively tied.
- Chase / Taylor / Amon-Ra / Puka: bunched within 9 pts — any is fine, don't agonize.
- Henry / Cook / Jacobs close out round-1 value.

## Room-value adjustment

When two names are close, prefer the one the room also values — the room's
sleepers come back around. Henry (room 28) and Jacobs (42) routinely survive
into rounds 2-3 in simulation, and even Jeanty (14), Achane (12), and CMC (11)
often last past the early picks. Chase, Puka, and Amon-Ra (room 3-6) will not
come back. So among near-ties, take the room favorite and let the sleeper wait.

## Never in round 1

- QB: best gain is Josh Allen at just 87 (310 pts against a 251 QB wire) —
  rank 341 overall.
- TE: Bowers' projection has collapsed (53 pts); the top TE is now Trey McBride,
  gain 166, rank 31 overall — fine at rounds 3-4, not here.
