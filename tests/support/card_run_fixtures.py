"""Product-run records for file-only lookup and card rendering contracts."""

from dataclasses import replace

from ummanu.webproto.runs import ProductRun


def record(run_id="pr-one", ref="ummanu-1", **changes):
    return replace(ProductRun(run_id, "request/雪", ref, "ummanu", "worker", "profile"), **changes)
