"""`traderos config set | show`: local settings (traderos.settings). Secrets are shown masked, never in full."""
from __future__ import annotations

from traderos import settings
from traderos.cli import Output
from traderos.store import Store


def register(sub) -> None:
    p = sub.add_parser("config", help="local settings")
    cs = p.add_subparsers(dest="config_command", required=True)
    x = cs.add_parser("set", help="set a setting")
    x.add_argument("key", choices=settings.KEYS)
    x.add_argument("value")
    x.set_defaults(run=config_set)
    x = cs.add_parser("show", help="show settings (secrets masked)")
    x.set_defaults(run=config_show)


def config_set(args) -> Output:
    store = Store(args.home).init()
    path = settings.write(store.home, args.key, args.value)
    text = f"Saved {args.key} in {path} (owner-only)."
    if args.key in settings.SECRETS:               # a secret is only ever shown masked; any other value is not echoed
        text += f" Shown masked: {settings.mask(args.key, args.value)}."
    return Output({"set": args.key}, text)


def config_show(args) -> Output:
    store = Store(args.home).init()
    data = {k: settings.mask(k, v) for k, v in sorted(settings.read(store.home).items())}
    return Output(data, "\n".join(f"{k} = {v}" for k, v in data.items()) or "No settings yet.")


__all__ = ["register"]
