CREATE OR REPLACE VIEW ORDERS_BOOK COPY GRANTS AS
-- The master "orders book": one row per OPEN order at the client's latest
-- snapshot. Reproduces the looker-staffing-predictive `overview` +
-- `health_distribution` + `predictive_analytics_table` grain in one place, so a
-- single semantic view powers the Performance-overview KPIs, the risk/health
-- distribution, breakdown/performance-analysis group-bys, and the order-detail
-- table. Risk band label is range-joined per-client from config (the LookML
-- risk_label view); fill/TTF band labels are the fact's own precomputed columns.
WITH config AS (
    SELECT entity_id, PARSE_JSON(config) AS config
    FROM {{ source_db }}.MODELLED.CAMPAIGN_MANAGEMENT_CONFIG_STORE
    WHERE entity_type = 'client' AND key = 'predictiveAnalyticsSettings'
),
risk_band AS (
    SELECT
        entity_id AS client_id,
        r.key::string AS label,
        r.value:"min"::float AS mn,
        -- LookML edge fix: top band max 1.0 -> 1.0001 so score = 1.0 still lands.
        CASE WHEN r.value:"max"::float = 1.0 THEN 1.0001 ELSE r.value:"max"::float END AS mx
    FROM config, LATERAL FLATTEN(input => config:riskScore) r
),
max_date AS (
    SELECT CLIENT_ID, MAX(DATE(DATE_ADDED)) AS d
    FROM {{ source_db }}.MODELLED.UA_PREDICTIVE_ANALYTICS_JOB_MERTICS_PREDICTIONS
    WHERE IS_ORDER_OPEN = TRUE
    GROUP BY CLIENT_ID
),
latest AS (
    SELECT j.*
    FROM {{ source_db }}.MODELLED.UA_PREDICTIVE_ANALYTICS_JOB_MERTICS_PREDICTIONS j
    JOIN max_date m ON j.CLIENT_ID = m.CLIENT_ID AND DATE(j.DATE_ADDED) = m.d
    WHERE j.IS_ORDER_OPEN = TRUE
)
SELECT
    j.CLIENT_ID,
    j.ORDER_ID,
    -- attributes (pivot dimensions for distribution/breakdown + detail table)
    j.ORDER_TITLE,
    j.ORDER_COMPANY,
    j.ORDER_CATEGORY,
    j.ORDER_TYPE,
    j.ORDER_ROLE,
    j.ORDER_LOCATION,
    j.ORDER_COUNTRY,
    j.REGION,
    j.COST_CENTER,
    j.BRANCH_NAME,
    j.SUBBRAND,
    j.CAMPAIGN_NAME,
    j.ORDER_STATUS,
    -- predictions (values)
    j.DIFFICULTY_SCORE                              AS RISK_SCORE,
    j.NEXT_HIRE_FILLED_IN_30_DAYS_PREDICTED         AS FILL_PROBABILITY,
    j.NEXT_HIRE_TIME_TO_FILL_PREDICTED              AS TIME_TO_FILL,
    -- band labels
    COALESCE(rb.label, 'UNKNOWN')                   AS RISK_GROUP_LABEL,
    j.NEXT_HIRE_FILLED_IN_30_DAYS_LABEL             AS FILL_GROUP_LABEL,
    j.NEXT_HIRE_TIME_TO_FILL_LABEL                  AS TTF_GROUP_LABEL,
    j.JOB_HEALTH_STATUS,
    -- health + ops
    j.JOB_HEALTH_SCORE,
    DATEDIFF('day', j.EFFECTIVE_POSTED_DATE, j.DATE_ADDED) AS DAYS_OPEN,
    j.ACTUAL_TOTAL_POSITIONS                        AS POSITIONS,
    j.SALARY_BASE_RATE                              AS SALARY,
    j.START_COUNT                                   AS HIRES,
    j.JOVEO_SPEND                                   AS SPEND,
    j.CURRENCY_CODE,
    -- risky flag (High or Very High risk)
    CASE WHEN UPPER(COALESCE(rb.label, '')) IN ('HIGH RISK', 'VERY HIGH RISK')
         THEN 1 ELSE 0 END                          AS IS_RISKY
FROM latest j
LEFT JOIN risk_band rb
    ON j.CLIENT_ID = rb.client_id
   AND j.DIFFICULTY_SCORE >= rb.mn
   AND j.DIFFICULTY_SCORE <  rb.mx
;
