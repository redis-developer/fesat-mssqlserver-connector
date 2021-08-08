import os
import random
import time
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory

import assertpy
from mssqlserver_connector.mssqlserver import MsSqlServerOfflineStoreConfig
from mssqlserver_connector.mssqlserver_source import MsSqlServerSource
import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from pytz import utc
import sqlalchemy

import feast.driver_test_data as driver_data

from feast import (
    RepoConfig,
    errors,
    utils,
)
from feast.entity import Entity
from feast.feature import Feature
from feast.feature_store import FeatureStore
from feast.feature_view import FeatureView
from feast.infra.online_stores.sqlite import SqliteOnlineStoreConfig
from feast.infra.offline_stores.offline_utils import (
    DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL,
)
from feast.value_type import ValueType
from sqlalchemy.engine import create_engine

np.random.seed(0)

PROJECT_NAME = "default"


def generate_entities(date, infer_event_timestamp_col, order_count: int = 1000):
    end_date = date
    before_start_date = end_date - timedelta(days=365)
    start_date = end_date - timedelta(days=7)
    after_end_date = end_date + timedelta(days=365)
    customer_entities = list(range(1001, 1110))
    driver_entities = list(range(5001, 5110))
    orders_df = driver_data.create_orders_df(
        customers=customer_entities,
        drivers=driver_entities,
        start_date=before_start_date,
        end_date=after_end_date,
        order_count=order_count,
        infer_event_timestamp_col=infer_event_timestamp_col,
    )
    return customer_entities, driver_entities, end_date, orders_df, start_date


def create_driver_hourly_stats_feature_view(source):
    driver_stats_feature_view = FeatureView(
        name="driver_stats",
        entities=["driver"],
        features=[
            Feature(name="conv_rate", dtype=ValueType.FLOAT),
            Feature(name="acc_rate", dtype=ValueType.FLOAT),
            Feature(name="avg_daily_trips", dtype=ValueType.INT32),
        ],
        batch_source=source,
        ttl=timedelta(hours=2),
    )
    return driver_stats_feature_view



def create_customer_daily_profile_feature_view(source):
    customer_profile_feature_view = FeatureView(
        name="customer_profile",
        entities=["customer_id"],
        features=[
            Feature(name="current_balance", dtype=ValueType.FLOAT),
            Feature(name="avg_passenger_count", dtype=ValueType.FLOAT),
            Feature(name="lifetime_trip_count", dtype=ValueType.INT32),
            Feature(name="avg_daily_trips", dtype=ValueType.INT32),
        ],
        batch_source=source,
        ttl=timedelta(days=2),
    )
    return customer_profile_feature_view


# Converts the given column of the pandas records to UTC timestamps
def convert_timestamp_records_to_utc(records, column):
    for record in records:
        record[column] = utils.make_tzaware(record[column]).astimezone(utc)
    return records


# Find the latest record in the given time range and filter
def find_asof_record(records, ts_key, ts_start, ts_end, filter_key, filter_value):
    found_record = {}
    for record in records:
        if record[filter_key] == filter_value and ts_start <= record[ts_key] <= ts_end:
            if not found_record or found_record[ts_key] < record[ts_key]:
                found_record = record
    return found_record


def get_expected_training_df(
    customer_df: pd.DataFrame,
    customer_fv: FeatureView,
    driver_df: pd.DataFrame,
    driver_fv: FeatureView,
    orders_df: pd.DataFrame,
    event_timestamp: str,
    full_feature_names: bool = False,
):
    # Convert all pandas dataframes into records with UTC timestamps
    order_records = convert_timestamp_records_to_utc(
        orders_df.to_dict("records"), event_timestamp
    )
    driver_records = convert_timestamp_records_to_utc(
        driver_df.to_dict("records"), driver_fv.batch_source.event_timestamp_column
    )
    customer_records = convert_timestamp_records_to_utc(
        customer_df.to_dict("records"), customer_fv.batch_source.event_timestamp_column
    )

    # Manually do point-in-time join of orders to drivers and customers records
    for order_record in order_records:
        driver_record = find_asof_record(
            driver_records,
            ts_key=driver_fv.batch_source.event_timestamp_column,
            ts_start=order_record[event_timestamp] - driver_fv.ttl,
            ts_end=order_record[event_timestamp],
            filter_key="driver_id",
            filter_value=order_record["driver_id"],
        )
        customer_record = find_asof_record(
            customer_records,
            ts_key=customer_fv.batch_source.event_timestamp_column,
            ts_start=order_record[event_timestamp] - customer_fv.ttl,
            ts_end=order_record[event_timestamp],
            filter_key="customer_id",
            filter_value=order_record["customer_id"],
        )

        order_record.update(
            {
                (f"driver_stats__{k}" if full_feature_names else k): driver_record.get(
                    k, None
                )
                for k in ("conv_rate", "avg_daily_trips")
            }
        )

        order_record.update(
            {
                (
                    f"customer_profile__{k}" if full_feature_names else k
                ): customer_record.get(k, None)
                for k in (
                    "current_balance",
                    "avg_passenger_count",
                    "lifetime_trip_count",
                )
            }
        )

    # Convert records back to pandas dataframe
    expected_df = pd.DataFrame(order_records)

    # Move "event_timestamp" column to front
    current_cols = expected_df.columns.tolist()
    current_cols.remove(event_timestamp)
    expected_df = expected_df[[event_timestamp] + current_cols]

    # Cast some columns to expected types, since we lose information when converting pandas DFs into Python objects.
    if full_feature_names:
        expected_column_types = {
            "order_is_success": "int32",
            "driver_stats__conv_rate": "float32",
            "customer_profile__current_balance": "float32",
            "customer_profile__avg_passenger_count": "float32",
        }
    else:
        expected_column_types = {
            "order_is_success": "int32",
            "conv_rate": "float32",
            "current_balance": "float32",
            "avg_passenger_count": "float32",
        }

    for col, typ in expected_column_types.items():
        expected_df[col] = expected_df[col].astype(typ)

    return expected_df


@pytest.mark.integration
@pytest.mark.parametrize(
    "provider_type", ["local"],
)
@pytest.mark.parametrize(
    "infer_event_timestamp_col", [False, True],
)
@pytest.mark.parametrize(
    "full_feature_names", [False, True],
)
def test_historical_features_from_mssqlserver_sources(
    provider_type, infer_event_timestamp_col, capsys, full_feature_names
):
    offline_store = MsSqlServerOfflineStoreConfig()

    # Connect once to create the test database.
    conn_str = "mssql+pyodbc://sa:yourStrong(!)Password@localhost:1433/master?driver=ODBC+Driver+17+for+SQL+Server"
    engine = create_engine(conn_str, connect_args={'autocommit':True})

    try:
        engine.execute("CREATE DATABASE feast_test")
    except sqlalchemy.exc.ProgrammingError:
        # Database probably already exists.
        pass

    # Create an engine that connects directly to the new test database.
    engine = create_engine(offline_store.connection_string)

    # We need to tell SQLAlchemy explicitly about timestamp fields, or else it
    # will try to create "TIMESTAMP" columns, which don't work. We need DATETIME
    # columns.
    fields = {
        "e_ts":  sqlalchemy.dialects.mssql.DATETIMEOFFSET(),
        "event_timestamp":  sqlalchemy.dialects.mssql.DATETIMEOFFSET(),
        "event_timestamp":  sqlalchemy.dialects.mssql.DATETIMEOFFSET(),
        "created":  sqlalchemy.dialects.mssql.DATETIMEOFFSET(),
    }

    start_date = datetime.now().replace(microsecond=0, second=0, minute=0)
    (
        customer_entities,
        driver_entities,
        end_date,
        orders_df,
        start_date,
    ) = generate_entities(start_date, infer_event_timestamp_col)

    mssqlserver_table_prefix = (
        f"test_hist_retrieval_{int(time.time_ns())}_{random.randint(1000, 9999)}"
    )

    # Stage orders_df to SQL Server
    table_name = f"{mssqlserver_table_prefix}_orders"
    entity_df_query = f"SELECT * FROM {table_name}"
    orders_df.to_sql(table_name, engine, if_exists="replace", dtype=fields)

    # Stage driver_df to SQL Server
    driver_df = driver_data.create_driver_hourly_stats_df(
        driver_entities, start_date, end_date
    )
    driver_table_name = f"{mssqlserver_table_prefix}_driver_hourly"
    driver_df.to_sql(driver_table_name, engine, if_exists="replace", dtype=fields)

    # Stage customer_df to SQL Server
    customer_df = driver_data.create_customer_daily_profile_df(
        customer_entities, start_date, end_date
    )
    customer_table_name = f"{mssqlserver_table_prefix}_customer_profile"
    customer_df.to_sql(customer_table_name, engine, if_exists="replace", dtype=fields)


    with TemporaryDirectory() as temp_dir:
        driver_source = MsSqlServerSource(
            table_ref=driver_table_name,
            event_timestamp_column="event_timestamp",
            created_timestamp_column="created",
        )
        driver_fv = create_driver_hourly_stats_feature_view(driver_source)

        customer_source = MsSqlServerSource(
            table_ref=customer_table_name,
            event_timestamp_column="event_timestamp",
            created_timestamp_column="created",
        )
        customer_fv = create_customer_daily_profile_feature_view(customer_source)

        driver = Entity(name="driver", join_key="driver_id", value_type=ValueType.INT64)
        customer = Entity(name="customer_id", value_type=ValueType.INT64)

        if provider_type == "local":
            store = FeatureStore(
                config=RepoConfig(
                    registry=os.path.join(temp_dir, "registry.db"),
                    project="default",
                    provider="local",
                    online_store=SqliteOnlineStoreConfig(
                        path=os.path.join(temp_dir, "online_store.db"),
                    ),
                    offline_store=offline_store,
                )
            )
        else:
            raise Exception("Invalid provider used as part of test configuration")

        store.apply([driver, customer, driver_fv, customer_fv])

        try:
            event_timestamp = (
                DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL
                if DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL in orders_df.columns
                else "e_ts"
            )
            expected_df = get_expected_training_df(
                customer_df,
                customer_fv,
                driver_df,
                driver_fv,
                orders_df,
                event_timestamp,
                full_feature_names,
            )

            job_from_sql = store.get_historical_features(
                entity_df=entity_df_query,
                features=[
                    "driver_stats:conv_rate",
                    "driver_stats:avg_daily_trips",
                    "customer_profile:current_balance",
                    "customer_profile:avg_passenger_count",
                    "customer_profile:lifetime_trip_count",
                ],
                full_feature_names=full_feature_names,
            )

            start_time = datetime.utcnow()
            actual_df_from_sql_entities = job_from_sql.to_df()
            # actual_df_from_sql_entities.drop(columns=['index', 'entity_row_unique_id'],
            #                                  inplace=True)
            end_time = datetime.utcnow()
            with capsys.disabled():
                print(
                    str(
                        f"\nTime to execute job_from_sql.to_df() = '{(end_time - start_time)}'"
                    )
                )

            # assert sorted(expected_df.columns) == sorted(actual_df_from_sql_entities.columns)

            # XXX: Why aren't the frames equal?
            assert_frame_equal(
                expected_df.sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                ).reset_index(drop=True),
                actual_df_from_sql_entities[expected_df.columns]
                .sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                )
                .reset_index(drop=True),
                check_dtype=False,
            )

            table_from_sql_entities = job_from_sql.to_arrow()
            assert_frame_equal(
                actual_df_from_sql_entities.sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                ).reset_index(drop=True),
                table_from_sql_entities.to_pandas()
                .sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                )
                .reset_index(drop=True),
            )

            timestamp_column = (
                "e_ts"
                if infer_event_timestamp_col
                else DEFAULT_ENTITY_DF_EVENT_TIMESTAMP_COL
            )

            entity_df_query_with_invalid_join_key = (
                f"select order_id, driver_id, customer_id as customer, "
                f"order_is_success, {timestamp_column} FROM {table_name}"
            )
            # Rename the join key; this should now raise an error.
            assertpy.assert_that(
                store.get_historical_features(
                    entity_df=entity_df_query_with_invalid_join_key,
                    features=[
                        "driver_stats:conv_rate",
                        "driver_stats:avg_daily_trips",
                        "customer_profile:current_balance",
                        "customer_profile:avg_passenger_count",
                        "customer_profile:lifetime_trip_count",
                    ],
                ).to_df
            ).raises(errors.FeastEntityDFMissingColumnsError).when_called_with()

            job_from_df = store.get_historical_features(
                entity_df=orders_df,
                features=[
                    "driver_stats:conv_rate",
                    "driver_stats:avg_daily_trips",
                    "customer_profile:current_balance",
                    "customer_profile:avg_passenger_count",
                    "customer_profile:lifetime_trip_count",
                ],
                full_feature_names=full_feature_names,
            )

            # Rename the join key; this should now raise an error.
            orders_df_with_invalid_join_key = orders_df.rename(
                {"customer_id": "customer"}, axis="columns"
            )
            assertpy.assert_that(
                store.get_historical_features(
                    entity_df=orders_df_with_invalid_join_key,
                    features=[
                        "driver_stats:conv_rate",
                        "driver_stats:avg_daily_trips",
                        "customer_profile:current_balance",
                        "customer_profile:avg_passenger_count",
                        "customer_profile:lifetime_trip_count",
                    ],
                ).to_df
            ).raises(errors.FeastEntityDFMissingColumnsError).when_called_with()

            start_time = datetime.utcnow()
            actual_df_from_df_entities = job_from_df.to_df()
            end_time = datetime.utcnow()
            with capsys.disabled():
                print(
                    str(
                        f"Time to execute job_from_df.to_df() = '{(end_time - start_time)}'\n"
                    )
                )

            assert sorted(expected_df.columns) == sorted(
                actual_df_from_df_entities.columns
            )
            assert_frame_equal(
                expected_df.sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                ).reset_index(drop=True),
                actual_df_from_df_entities[expected_df.columns]
                .sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                )
                .reset_index(drop=True),
                check_dtype=False,
            )

            table_from_df_entities = job_from_df.to_arrow()
            assert_frame_equal(
                actual_df_from_df_entities.sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                ).reset_index(drop=True),
                table_from_df_entities.to_pandas()
                .sort_values(
                    by=[event_timestamp, "order_id", "driver_id", "customer_id"]
                )
                .reset_index(drop=True),
            )
        finally:
            store.teardown()
