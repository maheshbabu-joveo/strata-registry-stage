CREATE OR REPLACE VIEW TRENDS_DAILY COPY GRANTS AS
-- One row per open order per snapshot day. Reproduces the LookML
-- `time_and_probability_trend` base grain so SV_TRENDS can serve the Time-to-fill
-- and Fill-probability trend series (query DIMENSIONS=[SNAPSHOT_DATE], the SV's
-- AVG metrics give the daily book average). ORDER_ROLE kept so the `order_purpose`
-- default (hiring = placement/likely_placement) can be applied as a filter.
SELECT
    CLIENT_ID,
    DATE(DATE_ADDED)                        AS SNAPSHOT_DATE,
    ORDER_ID,
    ORDER_ROLE,
    MD5(CONCAT_WS('|', CLIENT_ID, ORDER_ID, TO_VARCHAR(DATE(DATE_ADDED)))) AS ROW_ID,
    NEXT_HIRE_TIME_TO_FILL_PREDICTED        AS TIME_TO_FILL,
    NEXT_HIRE_FILLED_IN_30_DAYS_PREDICTED   AS FILL_PROBABILITY
FROM {{ source_db }}.MODELLED.UA_PREDICTIVE_ANALYTICS_JOB_MERTICS_PREDICTIONS
WHERE IS_ORDER_OPEN = TRUE
;
