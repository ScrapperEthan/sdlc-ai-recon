#!/usr/bin/env python3
"""
Retrieval-layer CLI — call any tool without an agent (stdlib only, read-only,
no DB, no credentials). An agent like opencode can call these via shell.

Examples:
  python cli.py impact mc-hk-hase-api-ingress-core --transitive
  python cli.py hubs --top 15
  python cli.py consumers otxBatchLetter
  python cli.py producers tracking
  python cli.py repo-routes mc-hk-hase-svc-bat-tracking-job
  python cli.py usecase --use-case-id UC123
  python cli.py search "publishIngressEvent" --glob "*.java"
  python cli.py read mc-hk-hase-ingress-api/src/main/java/.../IngressResource.java --start 60 --end 95
  python cli.py trace --use-case-id UC123
  python cli.py trace --destination otx_bat_letter
  python cli.py unified-impact IngressService --bundle ingress
  python cli.py unified-impact mc-hk-hase-ingress-api --transitive
"""
import argparse
import json
import sys

from retriever import graph, messages, code, flow, unified_impact


def _emit(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("impact", help="dependency blast radius of a repo")
    p.add_argument("repo")
    p.add_argument("--transitive", action="store_true")

    p = sub.add_parser("hubs", help="most depended-on repos")
    p.add_argument("--top", type=int, default=20)

    p = sub.add_parser("consumers", help="who consumes a queue/topic (substring)")
    p.add_argument("destination")

    p = sub.add_parser("producers", help="who produces to a queue/topic (substring)")
    p.add_argument("destination")

    p = sub.add_parser("repo-routes", help="all message edges touching a repo")
    p.add_argument("repo")

    p = sub.add_parser("usecase", help="use-case -> topic from the dev/SCT snapshot")
    p.add_argument("--use-case-id")
    p.add_argument("--topic")

    p = sub.add_parser("use-cases-for-topic", help="reverse: topic -> every use case (dev/SCT)")
    p.add_argument("topic")
    p.add_argument("--substring", action="store_true", help="substring match (default is exact)")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("search", help="grep the mirror")
    p.add_argument("pattern")
    p.add_argument("--glob", default="*.java")
    p.add_argument("--max", type=int, default=50)

    p = sub.add_parser("read", help="read line-numbered source from the mirror")
    p.add_argument("path")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int)

    p = sub.add_parser("trace", help="stitch use-case/destination across the async wiring")
    p.add_argument("--use-case-id")
    p.add_argument("--destination")

    p = sub.add_parser(
        "unified-impact", help="deps + async peers + callers, routed to a bundle's CodeGraph index"
    )
    p.add_argument("seed")
    p.add_argument("--bundle", help="routing hint; else routes by repo tag / staging dir")
    p.add_argument("--transitive", action="store_true")

    p = sub.add_parser("call-graph", help="raw codegraph explore, routed to the defining bundle")
    p.add_argument("query")

    args = ap.parse_args()

    if args.cmd == "impact":
        _emit(graph.impact(args.repo, args.transitive))
    elif args.cmd == "hubs":
        _emit(graph.hubs(args.top))
    elif args.cmd == "consumers":
        _emit(messages.who_consumes(args.destination))
    elif args.cmd == "producers":
        _emit(messages.who_produces(args.destination))
    elif args.cmd == "repo-routes":
        _emit(messages.routes_for_repo(args.repo))
    elif args.cmd == "usecase":
        _emit(messages.usecase_route(args.use_case_id, args.topic))
    elif args.cmd == "use-cases-for-topic":
        _emit(messages.reverse_lookup_use_cases(args.topic, not args.substring, args.limit))
    elif args.cmd == "search":
        for line in code.search_code(args.pattern, args.glob, args.max):
            print(line)
    elif args.cmd == "read":
        sys.stdout.write(code.read_file(args.path, args.start, args.end))
    elif args.cmd == "trace":
        _emit(flow.trace(args.use_case_id, args.destination))
    elif args.cmd == "unified-impact":
        _emit(unified_impact.query(args.seed, args.transitive, args.bundle))
    elif args.cmd == "call-graph":
        _emit(unified_impact.call_graph(args.query))


if __name__ == "__main__":
    main()
