"""`traderos export` and `traderos import`: portable bundles (spec 10 I07, 11.3; docs/cli-contract-v1.md)."""
from __future__ import annotations

import pathlib

from traderos.cli import Output
from traderos.contracts import ExportProfile
from traderos.store import Store

NOT_IN_THIS_RELEASE = "portable bundles: not in this release (they come in the first update)"


def register(sub) -> None:
    p = sub.add_parser("export", help=NOT_IN_THIS_RELEASE, description=NOT_IN_THIS_RELEASE)
    p.add_argument("passport_ids", nargs="*", help="Passports to export; none means everything (full_private)")
    p.add_argument("--profile", choices=[x.value for x in ExportProfile], default=ExportProfile.FULL_PRIVATE.value)
    p.add_argument("--out", help="where to write it (default ~/.traderos/exports/<bundle-id>.traderos)")
    p.add_argument("--now", help="the time to record in the bundle (RFC 3339); without it the bundle holds no time, "
                                 "so the same profile always exports the same bytes")
    p.set_defaults(run=export)
    p = sub.add_parser("import", help=NOT_IN_THIS_RELEASE, description=NOT_IN_THIS_RELEASE)
    p.add_argument("bundle")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only (the default)")
    mode.add_argument("--apply", action="store_true", help="import it")
    p.set_defaults(run=import_)


def export(args) -> Output:
    from traderos import transfer
    transfer.require_released("export")
    from traderos.contracts import parse_utc
    store = Store(args.home).init()
    now = parse_utc(args.now, field="--now") if args.now else None
    manifest, path = transfer.export(store, tuple(args.passport_ids), profile=args.profile,
                                     out=pathlib.Path(args.out) if args.out else None, now=now)
    text = [f"Wrote {path} ({manifest.export_profile.value}, {len(manifest.entries)} files, bundle "
            f"{manifest.bundle_id})."]
    text += [f"  not included: {o.ref} ({o.reason})" for o in manifest.omissions[:20]]
    if len(manifest.omissions) > 20:
        text.append(f"  ... and {len(manifest.omissions) - 20} more")
    text.append("No keys, tokens or settings are ever exported.")
    return Output({"bundle": manifest, "path": str(path)}, "\n".join(text))


def import_(args) -> Output:
    from traderos import transfer
    transfer.require_released("import")
    from traderos.contracts import ErrorCode, IntegrityError
    store = Store(args.home)                     # a preview never creates the profile; apply does, once it is ok
    report = transfer.import_bundle(store, pathlib.Path(args.bundle), apply=args.apply)
    if args.apply and not report.ok:
        raise IntegrityError(transfer.describe(report), code=ErrorCode.INTEGRITY_MISMATCH)
    return Output(report, transfer.describe(report))
