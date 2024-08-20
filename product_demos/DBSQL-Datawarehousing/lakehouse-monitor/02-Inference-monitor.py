# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # Lakehouse Monitoring Demo
# MAGIC
# MAGIC ## Inference Monitor 
# MAGIC In a machine learning project, you are likely curious about the quality of predictions over time and may like to compare them against your ground truth predictions. For use cases like this, you can create an inference monitor. 

# COMMAND ----------

# MAGIC %pip install "databricks-sdk>=0.28.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# If you haven't run 01-Timeseries-Monitor notebook before, run the following cell
# %run ./_resources/01-DataGeneration

# COMMAND ----------

# MAGIC %run ./config

# COMMAND ----------

# You must have `USE CATALOG` privileges on the catalog, and you must have `USE SCHEMA` privileges on the schema.
# If necessary, change the catalog and schema name here.

TABLE_NAME = f"{catalog}.{dbName}.silver_transaction"
TABLE_NAME_PREDICTIONS = f"{catalog}.{dbName}.silver_transaction_predictions"
BASELINE_PREDICTIONS = f"{catalog}.{dbName}.silver_predictions_baseline"

# Define the timestamp column name
TIMESTAMP_COL = "TransactionDate"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Create a Predictions Table
# MAGIC Create a dataset with predictions. The Inference Monitor uses a prediction column to calculate it's statistics, a model version column that acts like a slice, and, optionally, a label column for the ground truth value. We specify the names for each of these columns when we create the Inference Monitor.
# MAGIC
# MAGIC Below we are faking the data to keep things simple and show you the types that will work with the monitor. We also create a `Critical` column which flags when we're working with user ratings instead of admin ratings. We'll use this in a custom metric later.

# COMMAND ----------


import pyspark.sql.functions as F

(spark.table(TABLE_NAME)
   .withColumn("Prediction",  F.least(F.greatest(F.col("ProductRating") + F.randn(), F.lit(0.0)), F.lit(5.0)))
   .withColumn("ModelVersion", F.lit("1"))
   .withColumn("Critical", F.when(F.col("UserRole") == "Customer", F.lit(True)).otherwise(F.lit(False)))
   .write
   .option("overwriteSchema", "true")
   .mode("overwrite")
   .saveAsTable(TABLE_NAME_PREDICTIONS)
)
  
display(spark.sql(f"ALTER TABLE {TABLE_NAME_PREDICTIONS} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"))

# COMMAND ----------

display(spark.sql(f"SELECT ProductRating, Prediction, ModelVersion, Critical from {TABLE_NAME_PREDICTIONS};"))

# COMMAND ----------

# MAGIC %md
# MAGIC Use our predictions from the first day we started making predictions as the baseline. This will give us a comparison to see if the model's performance is decreasing over time.

# COMMAND ----------

display(spark.sql(f"""
  CREATE OR REPLACE VIEW {BASELINE_PREDICTIONS} AS 
  (SELECT * FROM {TABLE_NAME_PREDICTIONS} 
  WHERE date({TIMESTAMP_COL}) = (select min(date({TIMESTAMP_COL})) as min_date from {TABLE_NAME_PREDICTIONS})
  );"""))

# COMMAND ----------

display(spark.sql(f"SELECT * FROM {BASELINE_PREDICTIONS}"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create an Inference Log Monitor

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import MonitorTimeSeries, MonitorInferenceLog, MonitorInferenceLogProblemType
import os

# COMMAND ----------

# MAGIC %md
# MAGIC Note that if you create the monitor without a baseline table, you'll see comparisons to prior periods only. With a baseline table, you'll see the metric change relative to your baseline as well.

# COMMAND ----------

# Define time windows to aggregate metrics over
# Note that granularities must be subsets of 
# [5 minutes, 30 minutes, 1 hour, 1 day, 1 month, 1 year] 
# or multiples of [1 week]
GRANULARITIES = ["1 day", "1 week"]                       

# COMMAND ----------

# Create a monitor using a Timeseries profile type. After the intial refresh completes, you can view the autogenerated dashboard from the Quality tab of the table in Catalog Explorer. 
print(f"Creating monitor for {TABLE_NAME_PREDICTIONS}")

w = WorkspaceClient()

try:
  lhm_monitor = w.quality_monitors.create(
      table_name=TABLE_NAME_PREDICTIONS, # Always use 3-level namespace
      inference_log=MonitorInferenceLog(
          problem_type=MonitorInferenceLogProblemType.PROBLEM_TYPE_REGRESSION,
          prediction_col="Prediction",
          timestamp_col=TIMESTAMP_COL,
          granularities=GRANULARITIES,
          model_id_col="ModelVersion",
          label_col="ProductRating"
      ),
      baseline_table_name=BASELINE_PREDICTIONS,
      assets_dir = os.getcwd(),
      output_schema_name=f"{catalog}.{dbName}"
  )

except Exception as lhm_exception:
  print(lhm_exception)

# COMMAND ----------

# MAGIC %md
# MAGIC This next step waits for the monitor to be created and then waits for the initial calculation of metrics to complete.
# MAGIC
# MAGIC Note: It can take 10+ minutes to create and refresh the monitor

# COMMAND ----------

import time
from databricks.sdk.service.catalog import MonitorInfoStatus, MonitorRefreshInfoState

# Wait for monitor to be created
info = w.quality_monitors.get(table_name=f"{TABLE_NAME}")
while info.status == MonitorInfoStatus.MONITOR_STATUS_PENDING:
  info = w.quality_monitors.get(table_name=f"{TABLE_NAME}")
  time.sleep(10)

assert info.status == MonitorInfoStatus.MONITOR_STATUS_ACTIVE, "Error creating monitor"

refreshes = w.quality_monitors.list_refreshes(table_name=f"{TABLE_NAME}").refreshes
assert(len(refreshes) > 0)

run_info = refreshes[0]
while run_info.state in (MonitorRefreshInfoState.PENDING, MonitorRefreshInfoState.RUNNING):
  run_info = w.quality_monitors.get_refresh(table_name=f"{TABLE_NAME}", refresh_id=run_info.refresh_id)
  time.sleep(30)

assert run_info.state == MonitorRefreshInfoState.SUCCESS, "Monitor refresh failed"

# COMMAND ----------

# MAGIC %md
# MAGIC Let's view the profile and drift metrics.

# COMMAND ----------

# Display profile metrics table
profile_table = f"{TABLE_NAME_PREDICTIONS}_profile_metrics"  
display(spark.sql(f"SELECT * FROM {profile_table}"))

# Display the drift metrics table
drift_table = f"{TABLE_NAME_PREDICTIONS}_drift_metrics"
display(spark.sql(f"SELECT * FROM {drift_table}"))

# COMMAND ----------

# MAGIC %md
# MAGIC We can view some the summary statistics that show how well the model is performing.

# COMMAND ----------

display(spark.sql(f"SELECT window, mean_squared_error, r2_score from {profile_table} where mean_squared_error is not null"))

# COMMAND ----------

# MAGIC %md
# MAGIC View comparisons to our baseline day.

# COMMAND ----------

display(spark.sql(f"SELECT * from {drift_table} where drift_type = 'BASELINE';"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Add Custom Metrics

# COMMAND ----------

# MAGIC %md
# MAGIC We'll create two aggregate metrics and then one derived metric that will contain the weighted mean squared error. We're using the `Critical` column that we added when we created the predictions table to add extra weight to some of the predictions. Remember, we labelled a row as critical if it was for a customer instead of an admin.
# MAGIC
# MAGIC The two aggregate metrics will be the sum of weights and the weighted sum of squared prediction errors. Then we'll dive the weighted sum by the sum of the weights to get the weighted mean squared error.
# MAGIC

# COMMAND ----------

# MAGIC %md
# MAGIC Sum of the weights.

# COMMAND ----------

from databricks.sdk.service.catalog import MonitorMetric, MonitorMetricType
from pyspark.sql import types as T

weights_sum = MonitorMetric(
    type=MonitorMetricType.CUSTOM_METRIC_TYPE_AGGREGATE,
    name="weights_sum",
    input_columns=[":table"],
    definition="""sum(CASE
      WHEN {{prediction_col}} = {{label_col}} THEN 0
      WHEN {{prediction_col}} != {{label_col}} AND Critical=TRUE THEN 2
      ELSE 1 END)""",
    output_data_type=T.StructField("weights_sum", T.DoubleType()).json(),
)

# COMMAND ----------

# MAGIC %md
# MAGIC Weighted sum of the squared errors.

# COMMAND ----------

weighted_se = MonitorMetric(
    type=MonitorMetricType.CUSTOM_METRIC_TYPE_AGGREGATE,
    name="weighted_se",
    input_columns=[":table"],
    definition="""sum(CASE
      WHEN {{prediction_col}} = {{label_col}} THEN 0
      WHEN {{prediction_col}} != {{label_col}} AND Critical=TRUE THEN 2 * POWER({{prediction_col}} - {{label_col}}, 2)
      ELSE POWER({{prediction_col}} - {{label_col}}, 2) END)""",
    output_data_type=T.StructField("weighted_se", T.DoubleType()).json(),
)

# COMMAND ----------

# MAGIC %md
# MAGIC And the weighted mean squared error.

# COMMAND ----------

weighted_mse = MonitorMetric(
    type=MonitorMetricType.CUSTOM_METRIC_TYPE_DERIVED,
    name="weighted_mse",
    input_columns=[":table"],
    definition="""weighted_se / weights_sum""",
    output_data_type=T.StructField("weighted_mse", T.DoubleType()).json(),
)

# COMMAND ----------

# MAGIC %md
# MAGIC Let's also add in a drift metric that compares r2 scores across periods

# COMMAND ----------

r2_score_delta = MonitorMetric(
    type=MonitorMetricType.CUSTOM_METRIC_TYPE_DRIFT,
    name="r2_score_delta",
    input_columns=[":table"],
    definition="{{current_df}}.r2_score - {{base_df}}.r2_score",
    output_data_type=T.StructField("r2_score_delta", T.DoubleType()).json(),
)

# COMMAND ----------

# MAGIC %md
# MAGIC Update the monitor and add in the custom metrics.

# COMMAND ----------

try:
  lhm_monitor = w.quality_monitors.update(
      table_name=TABLE_NAME_PREDICTIONS, # Always use 3-level namespace
      inference_log=MonitorInferenceLog(
          problem_type=MonitorInferenceLogProblemType.PROBLEM_TYPE_REGRESSION,
          prediction_col="Prediction",
          timestamp_col=TIMESTAMP_COL,
          granularities=GRANULARITIES,
          model_id_col="ModelVersion",
          label_col="ProductRating", # optional
      ),
      custom_metrics=[weights_sum, weighted_se, weighted_mse, r2_score_delta],
      baseline_table_name=BASELINE_PREDICTIONS,
      output_schema_name=f"{catalog}.{dbName}"
  )

except Exception as lhm_exception:
  print(lhm_exception)

# COMMAND ----------

# MAGIC %md
# MAGIC Remember, we need to refresh after an update to see the new fields.

# COMMAND ----------

w = WorkspaceClient()
run_info = w.quality_monitors.run_refresh(TABLE_NAME_PREDICTIONS)

while run_info.state in (MonitorRefreshInfoState.PENDING, MonitorRefreshInfoState.RUNNING):
  run_info = w.quality_monitors.get_refresh(table_name=f"{TABLE_NAME_PREDICTIONS}", refresh_id=run_info.refresh_id)
  time.sleep(30)

assert run_info.state == MonitorRefreshInfoState.SUCCESS, "Monitor refresh failed"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. View Custom Metrics
# MAGIC And finally, let's view our custom metrics!

# COMMAND ----------

# MAGIC %md
# MAGIC Using the profile table, we can compare mean squared error to our weighted metric.

# COMMAND ----------

display(spark.sql(f"SELECT window, mean_squared_error, weights_sum, weighted_mse from {profile_table} where weights_sum is not null;"))

# COMMAND ----------

display(spark.sql(f"SELECT window, r2_score_delta from {drift_table} where drift_type = 'BASELINE';"))

# COMMAND ----------

# MAGIC %md
# MAGIC And using our drift table, we can see the change in r2 score.

# COMMAND ----------

display(spark.sql(f"SELECT window, window_cmp, r2_score_delta from {drift_table} where drift_type = 'CONSECUTIVE';"))

# COMMAND ----------

# Uncomment the following line of code to clean up the monitor (if you wish to run the quickstart on this table again).
# w.quality_monitors.delete(TABLE_NAME_PREDICTIONS)