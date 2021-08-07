# feast-mssqlserver-connector

A connector for SQL Server, Azure SQL Server, and Azure Synapse.

Contains:

* An offline store that supports pulling historical data from SQL Server, etc.

## Setup

Install the package like so:

	$ pip install -e ".[dev]"

## Running the Tests

Run the tests like this:

	$ pytest -x --integration tests/test_historical_retrieval.py

**NOTE**: Currently, the tests don't pass! Something is amiss: our dataframe
doesn't match the expected one.
