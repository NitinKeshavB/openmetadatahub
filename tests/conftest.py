from __future__ import annotations

import pytest


class FakeExecutor:
    """Returns canned rows for the first registered substring found in the SQL. Records every query."""

    def __init__(self) -> None:
        self.rules: list[tuple[str, object]] = []
        self.log: list[str] = []

    def when(self, needle: str, rows):
        self.rules.append((needle, rows))
        return self

    def query(self, sql: str):
        self.log.append(sql)
        for needle, rows in self.rules:
            if needle in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return []


@pytest.fixture
def fake():
    return FakeExecutor()


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    wh = tmp_path_factory.mktemp("warehouse")
    s = (SparkSession.builder.master("local[2]").appName("omh-tests")
         .config("spark.sql.warehouse.dir", str(wh)).config("spark.ui.enabled", "false")
         .config("spark.sql.shuffle.partitions", "2").config("spark.driver.bindAddress", "127.0.0.1")
         .getOrCreate())
    s.sparkContext.setLogLevel("ERROR")
    yield s
    s.stop()


CONTRACT_V1 = """
apiVersion: v3.1.0
kind: DataContract
id: orders-contract
name: Orders
version: 1.0.0
status: active
domain: sales
schema:
  - name: orders
    physicalName: main.sales.orders
    properties:
      - name: order_id
        logicalType: integer
        primaryKey: true
        required: true
      - name: email
        logicalType: string
        classification: pii
        quality:
          - type: library
            metric: invalidValues
            arguments: {pattern: "^[^@]+@[^@]+$"}
            mustBe: 0
            unit: rows
      - name: status
        logicalType: string
        required: true
        quality:
          - type: library
            metric: invalidValues
            arguments: {validValues: [NEW, PAID, SHIPPED]}
            mustBeLessThan: 1
            unit: percent
            severity: warning
      - name: updated_at
        logicalType: timestamp
    quality:
      - type: library
        metric: rowCount
        mustBeGreaterThan: 0
      - type: sql
        query: SELECT COUNT(*) FROM main.sales.orders WHERE amount < 0
        mustBe: 0
slaProperties:
  - property: freshness
    value: 24
    unit: h
    element: orders.updated_at
roles:
  - role: sales_analysts
    access: read
  - role: sales_etl
    access: write
"""
