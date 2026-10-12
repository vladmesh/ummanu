"""The one-shot transition of this installation to the product's new name (`docs/RENAME.md` §T3).

Class T in §T5: with its shell bootstrap and its tests, the only code allowed to name the old
product after the rename. It reaches the product only through relative imports of stable
primitives, so the rename card can move it with the package without rewriting it, and every old
and new name comes from `names`. Deleted in the packaging sprint, once rollback stops mattering.
"""
