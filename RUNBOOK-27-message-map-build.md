# RUNBOOK 27 (INTERNAL Codex / you) — run the message-map extractor + report coverage

> **Who runs: you or INTERNAL Codex on the box (NO LLM quota needed).** **Pull `master` first.**
> Read-only over the extract; writes only `index/message_edges.csv` + `index/message_channels.json`.
> First cut of the message map, built against the RUNBOOK-26 discovery (config-driven topics, HRN
> naming with the channel in the name). **Relay the coverage numbers + a few sample rows.**

## Step 1 — run it against the full extract
```
python make_message_map.py --mirror "C:\Users\45509915\Downloads\HASE_MDC"
```
It prints a coverage table and writes the two artifacts. Paste the table:
```
repos_with_destinations           [ how many repos declared a topic/queue ]
repos_with_channel_via_msg        [ how many got a channel from a destination name ]
channel_unknown_now_covered       [ name-channel-unknown repos that messaging now gives a channel ]
```
The headline we want: **`channel_unknown_now_covered` is large** — those are the messaging-only repos
that Maven couldn't reach. Every one of them is a repo that moves from "dark" to "serves channel X".

## Step 2 — eyeball a few extractions (sanity)
```
python -c "import json;d=json.load(open('index/message_channels.json',encoding='utf-8'))['repos'];import itertools;[print(k,'->',v['channels'],'|',(v['destinations'][0]['name'] if v['destinations'] else ''),(v['destinations'][0]['role'] if v['destinations'] else '')) for k,v in itertools.islice(sorted(d.items()),15)]"
```
Check: do the channels look right for the repo names? Are topics the HRN dotted ones? Any obvious misses
(a repo you KNOW does SMS that shows no channel)?

## Step 3 — spot-check the edges file
```
python -c "import csv;rows=list(csv.DictReader(open('index/message_edges.csv',encoding='utf-8')));print('edge rows',len(rows));[print(r['producer_repo'],'->',r['destination'],'->',r['consumer_repo']) for r in rows[:12]]"
```

## Send back
```
Step 1 coverage:  [ the 3 numbers ]
Step 2 samples:   [ ~10 repo -> channels lines; do they look right? ]
Step 3 edges:     [ edge count + a few rows ]
Misses/surprises: [ a repo you expected to have a channel but shows none; any wrong channel ]
```

## What this establishes (and what's next)
Green = the ~214 messaging-only repos start getting a channel from their **actual Kafka/JMS wiring**, not
just Maven. This is the first cut — direction (produce vs consume) is best-effort from nearby markers, and
`${placeholder}` queues aren't yet resolved to their config value. From your samples I'll tighten the
extractor, then **fold these channels into `serves_channels`** (so `/outage-impact` and the 仓库全景
"其他" lane shrink as those repos light up) and wire `make_message_map.py` into `refresh.py`.

## Honesty / limits
- First-cut roles are heuristic; the repo→destination→channel mapping is the reliable part.
- `${app.listener.*.queue}` placeholders are captured as the placeholder unless the literal also appears;
  resolving them to `listener.<x>.queue:` config values is the next tightening.
- Topics with no channel token and no known vendor (e.g. `…default-omni`) stay channel-less — honest.
