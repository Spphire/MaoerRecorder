"""MaoerRecorder CLI."""
from __future__ import annotations

import argparse
import sys

from maoer.api import MaoerApi
from maoer.auth import CookieJar, open_context, warmup
from maoer.config import load as load_config
from maoer.log import setup as setup_log
from maoer.orchestrator import Orchestrator


def _cmd_record(args: argparse.Namespace) -> int:
    cfg = load_config(args.room)
    Orchestrator(cfg).run()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.room)
    with open_context(cfg, headless=True) as ctx:
        warmup(ctx)
        jar = CookieJar(ctx)
        api = MaoerApi(cfg, jar, ctx)
        try:
            live, info = api.live_info()
            print(f"room_id     : {cfg.room_id}")
            print(f"broadcasting: {live}")
            if info:
                print(f"creator     : {api.creator_name(info)}")
                print(f"hls_url     : {api.hls_url(info)}")
        finally:
            api.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_log()
    parser = argparse.ArgumentParser(prog="maoer", description="MaoerRecorder")
    sub = parser.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--room", type=int, default=None, help="直播间 ID")
    common.add_argument("--managed-instance", default=None, help=argparse.SUPPRESS)

    p_rec = sub.add_parser("record", parents=[common], help="开始录制")
    p_rec.set_defaults(func=_cmd_record)

    p_st = sub.add_parser("status", parents=[common], help="查看状态")
    p_st.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
