CREATE OR REPLACE VIEW ADVISOR_ORDERS_BOOK COPY GRANTS AS
-- FIRST SLICE: just enough for the Today KpiStrip scalar KPIs.
-- One row per OPEN order at its latest snapshot, per client, with the per-client
-- risk band label inlined from CAMPAIGN_MANAGEMENT_CONFIG_STORE (the range-join
-- Looker did per-explore). Feeds SV_ADVISOR_ORDERS (METRICS mode -> KpiStrip).
-- We deliberately keep this lean; order attributes / spend / currency get added
-- once the KPI slice is live end-to-end and we replicate the pattern.
WITH config AS (
    SELECT entity_id, PARSE_JSON(config) AS config
    FROM {{ source_db }}.MODELLED.CAMPAIGN_MANAGEMENT_CONFIG_STORE
    WHERE entity_type = 'client' AND key = 'predictiveAnalyticsSettings'
),
risk_band AS (
    SELECT
        entity_id AS client_id,
        r.key::string AS risk_score_label,
        r.value:"min"::float AS min_val,
        CASE WHEN r.value:"max"::float = 1.0 THEN 1.0001 ELSE r.value:"max"::float END AS max_val
    FROM config, LATERAL FLATTEN(input => config:riskScore) r
),
max_job_date AS (
    SELECT CLIENT_ID, MAX(DATE(DATE_ADDED)) AS max_date
    FROM {{ source_db }}.MODELLED.UA_PREDICTIVE_ANALYTICS_JOB_MERTICS_PREDICTIONS
    WHERE IS_ORDER_OPEN = TRUE
    GROUP BY CLIENT_ID
),
latest_jobs AS (
    SELECT j.*
    FROM {{ source_db }}.MODELLED.UA_PREDICTIVE_ANALYTICS_JOB_MERTICS_PREDICTIONS j
    JOIN max_job_date md
        ON j.CLIENT_ID = md.CLIENT_ID
       AND DATE(j.DATE_ADDED) = md.max_date
    WHERE j.IS_ORDER_OPEN = TRUE
)
SELECT
    j.CLIENT_ID,
    j.ORDER_ID,
    j.DIFFICULTY_SCORE,
    j.NEXT_HIRE_FILLED_IN_30_DAYS_PREDICTED,
    j.NEXT_HIRE_TIME_TO_FILL_PREDICTED,
    COALESCE(rb.risk_score_label, 'UNKNOWN') AS RISK_SCORE_LABEL,
    CASE WHEN UPPER(COALESCE(rb.risk_score_label, '')) IN ('HIGH RISK', 'VERY HIGH RISK')
         THEN 1 ELSE 0 END                   AS IS_RISKY
FROM latest_jobs j
LEFT JOIN risk_band rb
    ON j.CLIENT_ID = rb.client_id
   AND j.DIFFICULTY_SCORE >= rb.min_val
   AND j.DIFFICULTY_SCORE <  rb.max_val
;
