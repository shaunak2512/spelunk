"""Schema discovery — the virtual filesystem's `ls` and `cat` (Wave 2)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import inspect

from .types import ColumnInfo, ColumnProfile, ForeignKey, IndexInfo, TableDescription, TableInfo

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def list_objects(engine: "Engine") -> list[TableInfo]:
    """List tables and views (the 'files'). Use a SQLAlchemy ``Inspector``.

    Tables get a ``row_count`` from a single-connection COUNT sweep; views are left ``None``
    because view queries can be arbitrarily expensive.
    """
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    view_names = inspector.get_view_names()

    result: list[TableInfo] = []
    with engine.connect() as conn:
        for name in table_names:
            tbl = sa.table(name)
            count: int = conn.execute(sa.select(sa.func.count()).select_from(tbl)).scalar() or 0
            result.append(TableInfo(name=name, kind="table", row_count=count))

    for name in view_names:
        result.append(TableInfo(name=name, kind="view"))

    return result


def describe(engine: "Engine", table: str, *, profile: bool = True) -> TableDescription:
    """Describe one table: columns, PK, FKs, and a small sample of rows.

    When ``profile=True``, also populate per-column ``ColumnProfile`` (null_fraction,
    distinct_count, sample_values) via plain aggregate SELECTs.
    Cap ``sample_rows`` small (e.g. 5).
    """
    inspector = inspect(engine)

    # -- Columns ----------------------------------------------------------------
    raw_cols = inspector.get_columns(table)
    pk_info = inspector.get_pk_constraint(table)
    pk_cols: list[str] = pk_info.get("constrained_columns", []) if pk_info else []

    columns: list[ColumnInfo] = []
    for col in raw_cols:
        columns.append(
            ColumnInfo(
                name=col["name"],
                type=str(col["type"]),
                nullable=bool(col.get("nullable", True)),
                primary_key=col["name"] in pk_cols,
                comment=col.get("comment"),
            )
        )

    # -- Indexes ----------------------------------------------------------------
    indexes: list[IndexInfo] = [
        IndexInfo(
            name=idx.get("name"),
            unique=bool(idx.get("unique", False)),
            columns=idx.get("column_names", []),
        )
        for idx in inspector.get_indexes(table)
    ]

    # -- Foreign keys -----------------------------------------------------------
    raw_fks = inspector.get_foreign_keys(table)
    foreign_keys: list[ForeignKey] = []
    for fk in raw_fks:
        # Each FK may cover multiple columns; zip them pairwise.
        for local_col, ref_col in zip(
            fk.get("constrained_columns", []),
            fk.get("referred_columns", []),
        ):
            foreign_keys.append(
                ForeignKey(
                    column=local_col,
                    ref_table=fk["referred_table"],
                    ref_column=ref_col,
                )
            )

    # -- Sample rows + row count ------------------------------------------------
    # Use a reflected Table object so SQLAlchemy handles identifier quoting.
    meta = sa.MetaData()
    tbl = sa.Table(table, meta, autoload_with=engine)

    with engine.connect() as conn:
        # Sample rows
        sample_q = sa.select(tbl).limit(5)
        raw_rows = conn.execute(sample_q).fetchall()
        col_keys = [c.key for c in tbl.columns]
        sample_rows: list[dict[str, Any]] = [
            dict(zip(col_keys, row)) for row in raw_rows
        ]

        # Row count (needed for null_fraction denominator)
        count_q = sa.select(sa.func.count()).select_from(tbl)
        row_count: int = conn.execute(count_q).scalar() or 0

        # -- Profile (per-column stats) -----------------------------------------
        profile_list: list[ColumnProfile] = []
        if profile:
            for col_obj in tbl.columns:
                col_name = col_obj.key

                # null_fraction = (COUNT(*) - COUNT(col)) / COUNT(*)
                if row_count > 0:
                    null_count_expr = row_count - sa.func.count(col_obj)
                    null_frac_q = sa.select(
                        null_count_expr.cast(sa.Float) / float(row_count)
                    ).select_from(tbl)
                    null_fraction: float | None = conn.execute(null_frac_q).scalar()
                else:
                    null_fraction = None

                # distinct_count
                distinct_q = sa.select(sa.func.count(sa.distinct(col_obj))).select_from(tbl)
                distinct_count: int | None = conn.execute(distinct_q).scalar()

                # sample_values: up to 5 distinct non-null values
                sample_vals_q = (
                    sa.select(col_obj)
                    .distinct()
                    .where(col_obj.isnot(None))
                    .limit(5)
                )
                sv_rows = conn.execute(sample_vals_q).fetchall()
                sample_values: list[Any] = [r[0] for r in sv_rows]

                profile_list.append(
                    ColumnProfile(
                        column=col_name,
                        null_fraction=null_fraction,
                        distinct_count=distinct_count,
                        sample_values=sample_values,
                    )
                )

    return TableDescription(
        name=table,
        columns=columns,
        primary_key=pk_cols,
        foreign_keys=foreign_keys,
        indexes=indexes,
        sample_rows=sample_rows,
        profile=profile_list,
        row_count=row_count,
    )
