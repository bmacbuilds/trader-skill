# Market Pulse

Needs S03 (Market Pulse) usable, and a dataset in the
store: it runs on the tutorial (daily bars, its seven synthetic symbols; breadth needs more symbols than that, and
the result says so).

A timestamped, feed-aware picture of the market in the person's own stored data: each symbol's last close and
change, breadth when the coverage is enough to say it, and a regime label with the method behind it.

```bash
traderos --json market
traderos --json market --dataset DS-...
traderos --json market --dataset DS-... --as-of 2026-10-19T14:15:00Z
```

`--dataset` can be left out when the store holds exactly one dataset. `--as-of` reads the data as it was at that
instant (RFC 3339); without it, the newest data.

## Steps

1. Run it on the dataset the person names, as of the time they care about.
2. Say the dataset, the as-of time, the provider and feed, the regime and its method, and breadth, each as printed.
   Say every STALE symbol and the completeness and limits that came back.
3. If coverage is too thin for breadth, the result says so: repeat that, never estimate breadth yourself.

## Rules

- Only the stored data, as of the time asked. It is never a live feed, and never called real time.
- A regime label is a description of the data, not a forecast.
- Every number comes from the result.

## What it never does

It never fetches data, never invents breadth, and never says what the market will do.
