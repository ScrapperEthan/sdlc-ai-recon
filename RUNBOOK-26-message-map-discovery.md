# RUNBOOK 26 (discovery) — surface the real async messaging patterns before building the message map

> **Who runs: you or INTERNAL Codex on the box (NO LLM/codex quota needed — these are plain `rg`
> greps).** Read-only over the extract. Goal: find out **how this codebase actually wires async
> messaging** (Kafka? SQS? Spring Cloud Stream? JMS?) and in what annotation/topic patterns — so the
> extractor I build next matches reality instead of guessing. This is the same discover-first move that
> de-risked the MDC sheet (RUNBOOK-11). **Relay the sample lines below.**

Set the extract path once (substitute if different):
```
# Git Bash
M="C:/Users/45509915/Downloads/HASE_MDC"
```
Each probe: run it, paste the **first ~15 lines**, and say **common / rare / absent**.

## Probe A — which messaging tech is actually used? (rough counts)
```
rg -c "@KafkaListener|KafkaTemplate|org.springframework.kafka" -g "*.java" "$M" | wc -l
rg -c "@SqsListener|SqsTemplate|AmazonSQS|amazonsqs|software.amazon.awssdk.*sqs" -g "*.java" "$M" | wc -l
rg -c "@StreamListener|StreamBridge|@Output|@Input|spring.cloud.stream" -g "*.java" "$M" | wc -l
rg -c "@JmsListener|JmsTemplate" -g "*.java" "$M" | wc -l
```
(Each number = how many files match. Tells us the dominant tech.)

## Probe B — consumer / listener sites (who RECEIVES)
```
rg -n "@KafkaListener|@SqsListener|@StreamListener|@JmsListener" -g "*.java" "$M" | head -25
```
Paste samples — I need to see how the **topic/queue is named** in the annotation (literal? `topics = "..."`?
a constant? a `${...}` property?).

## Probe C — producer / send sites (who SENDS)
```
rg -n "\.send\(|\.convertAndSend\(|streamBridge|EventProducer|publish" -g "*.java" "$M" | head -25
```
I need to see the **send call shape** and what identifies the destination (a constant, an enum, a literal).

## Probe D — how topics/queues are DECLARED
```
rg -n "[A-Z][A-Z0-9_]{3,}\s*\(\s*\"" -g "*Topic*.java" -g "*Queue*.java" -g "*Router*.java" "$M" | head -25
rg -n "\"[a-z0-9]+([._-][a-z0-9]+){2,}\"" -g "*.java" "$M" | rg -i "topic|queue|notif|sms|email|push" | head -25
```
(First = enum-style constants `NAME("literal")`; second = topic-looking string literals. Tells us the
naming convention, e.g. `hrn.hase.wpb.notification...` seen in the outage panel.)

## Probe E — config-driven routing (topics/queues in yml/properties)
```
rg -n "topic|queue|destination|kafka|sqs" -g "*.yml" -g "*.yaml" -g "*.properties" "$M" | head -30
```
Some destinations are config, not code — I need to know how much lives here vs in Java.

## Send back
For each probe: a handful of representative lines, plus one word — **common / rare / absent**. Especially:
1. **Dominant tech** (Kafka vs SQS vs Stream vs JMS — or a mix, and roughly the split).
2. **How a consumer names its topic** (literal / constant / `${property}`).
3. **How a producer names its destination** (constant / enum / literal).
4. **Topic literal convention** (e.g. the dotted `hrn.hase.…` form).

## Why this first (not just build it)
Everything we've built so far worked from repo **names** — safe to build blind. The message map must read
**actual source wiring**, which depends on these patterns. 30 minutes of greps here means the extractor I
write next is right the first time, instead of matching annotations that don't exist. After this I build
`make_message_edges.py` (source → producer/topic/consumer edges) and fold it into `serves_channels` /
outage so the ~214 messaging-only repos finally connect to channels.
