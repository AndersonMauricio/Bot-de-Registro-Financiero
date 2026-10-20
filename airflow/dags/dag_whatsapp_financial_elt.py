"""Airflow DAG for orchestrating WhatsApp Financial Data ELT and dbt transformations."""

from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.empty import EmptyOperator

default_args = {
    "owner": "data_engineering",
    "depends_on_past": False,
    "email_on_failure": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}

with DAG(
    dag_id="whatsapp_financial_analytics_elt",
    default_args=default_args,
    description="Incremental ELT pipeline from PostgreSQL to Data Warehouse marts via dbt",
    schedule_interval="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["finance", "whatsapp", "dbt", "elt"],
) as dag:

    start_task = EmptyOperator(task_id="start_pipeline")

    # In production with Astronomer Cosmos, DbtTaskGroup compiles dbt models into Airflow tasks
    # dbt_tg = DbtTaskGroup(
    #     group_id="dbt_transformations",
    #     project_config=ProjectConfig("/opt/airflow/dbt"),
    #     profile_config=profile_config,
    # )

    end_task = EmptyOperator(task_id="end_pipeline")

    start_task >> end_task
